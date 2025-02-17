import sys, os

cur_file_path = os.path.dirname(os.path.realpath(__file__))
sys.path.append(os.path.join(cur_file_path, ".."))

import importlib, time, math, shutil, json

import numpy as np

import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
from utils.logging import Logger, mkdir
from utils.transforms import rotation_matrix_to_angle_axis, batch_rodrigues

from datasets.amass_utils import CONTACT_INDS

from body_model.utils import SMPL_JOINTS, KEYPT_VERTS, smpl_to_openpose

from fitting.fitting_utils import (
    OP_IGNORE_JOINTS,
    parse_floor_plane,
    compute_cam2prior,
    OP_EDGE_LIST,
    log_cur_stats,
)
from fitting.fitting_loss import FittingLoss


LINE_SEARCH = "strong_wolfe"
J_BODY = len(SMPL_JOINTS) - 1  # no root

CONTACT_THRESH = 0.5


class MotionOptimizer:
    """Fits SMPL shape and motion to observation sequence"""

    def __init__(
        self,
        device,
        body_model,  # SMPL model to use (its batch_size should be B*T)
        num_betas,  # beta size in SMPL model
        batch_size,  # number of sequences to optimize
        seq_len,  # length of the sequences
        observed_modalities,  # list of the kinds of observations to use
        loss_weights,  # dict of weights for each loss term
        pose_prior,  # VPoser model
        motion_prior=None,  # humor model
        init_motion_prior=None,  # dict of GMM params to use for prior on initial motion state
        optim_floor=False,  # if true, optimize the floor plane along with body motion (need 2d observations)
        camera_matrix=None,  # camera intrinsics to use for reprojection if applicable
        robust_loss_type="none",
        robust_tuning_const=4.6851,
        joint2d_sigma=100,
        stage3_tune_init_state=True,
        stage3_tune_init_num_frames=15,
        stage3_tune_init_freeze_start=30,
        stage3_tune_init_freeze_end=50,
        stage3_contact_refine_only=False,
        use_chamfer=False,
        im_dim=(1080, 1080),
    ):  # image dimensions to use for visualization

        B, T = batch_size, seq_len
        self.batch_size = B
        self.seq_len = T
        self.body_model = body_model
        self.num_betas = num_betas
        self.optim_floor = optim_floor
        self.stage3_tune_init_state = stage3_tune_init_state
        self.stage3_tune_init_num_frames = stage3_tune_init_num_frames
        self.stage3_tune_init_freeze_start = stage3_tune_init_freeze_start
        self.stage3_tune_init_freeze_end = stage3_tune_init_freeze_end
        self.stage3_contact_refine_only = stage3_contact_refine_only
        self.im_dim = im_dim

        #
        # create the optimization variables
        #

        # number of states to explicitly optimize for
        # For first stages this will always be the full sequence
        num_state_steps = T
        # latent body pose
        self.pose_prior = pose_prior
        self.latent_pose_dim = self.pose_prior.latentD
        self.latent_pose = torch.zeros((B, num_state_steps, self.latent_pose_dim)).to(
            device
        )
        # root (global) transformation
        self.trans = torch.zeros((B, num_state_steps, 3)).to(device)
        self.root_orient = torch.zeros((B, num_state_steps, 3)).to(
            device
        )  # aa parameterization
        self.root_orient[:, :, 0] = np.pi
        # body shape
        self.betas = torch.zeros((B, num_betas)).to(device)  # same shape for all steps

        self.motion_prior = motion_prior
        self.init_motion_prior = init_motion_prior
        self.latent_motion = None
        assert self.motion_prior is not None
        # need latent dynamics sequence as well
        self.latent_motion_dim = self.motion_prior.latent_size
        self.cond_prior = self.motion_prior.use_conditional_prior
        # additional optimization params to set later
        self.trans_vel = None
        self.root_orient_vel = None
        self.joints_vel = None

        self.init_fidx = np.zeros(
            (B)
        )  # the frame chosen to use for the initial state (first frame by default)

        self.cam_f = self.cam_center = None
        assert not self.optim_floor
        self.use_camera = self.cam_f is not None and self.cam_center is not None

        #
        # create the loss function
        #
        self.smpl2op_map = smpl_to_openpose(
            body_model.model_type,
            use_hands=False,
            use_face=False,
            use_face_contour=False,
            openpose_format="coco25",
        )
        self.fitting_loss = FittingLoss(
            loss_weights,
            self.init_motion_prior,
            self.smpl2op_map,
            OP_IGNORE_JOINTS,
            self.cam_f,
            self.cam_center,
            robust_loss_type,
            robust_tuning_const,
            joints2d_sigma=joint2d_sigma,
            use_chamfer=use_chamfer,
        ).to(device)

    def run(
        self,
        observed_data,
        data_fps=30,
        lr=1.0,
        num_iter=[30, 70, 70],
        lbfgs_max_iter=20,
        stages_res_out=None,
        fit_gender="neutral",
    ):
        assert not self.motion_prior.in_rot_rep == "6d"
        assert not self.motion_prior.use_smpl_joint_inputs

        assert len(num_iter) == 3

        num_iter = [1, 1, 1]

        per_stage_outputs = {}  # SMPL results after each stage

        #
        # Stage I: Only global root and orientation
        #
        Logger.log(
            "Optimizing stage 1 - global root translation and orientation for %d iterations..."
            % (num_iter[0])
        )
        self.fitting_loss.set_stage(0)
        self.trans.requires_grad = True
        self.root_orient.requires_grad = True
        self.betas.requires_grad = False
        self.latent_pose.requires_grad = False

        root_opt_params = [self.trans, self.root_orient]

        root_optim = torch.optim.LBFGS(
            root_opt_params, max_iter=lbfgs_max_iter, lr=lr, line_search_fn=LINE_SEARCH
        )
        for i in range(num_iter[0]):
            Logger.log("ITER: %d" % (i))
            self.fitting_loss.cur_optim_step = i
            stats_dict = None

            def closure():
                root_optim.zero_grad()

                pred_data = dict()
                # Use current params to go through SMPL and get joints3d, verts3d, points3d
                body_pose = self.latent2pose(self.latent_pose)
                pred_data, _ = self.smpl_results(
                    self.trans, self.root_orient, body_pose, self.betas
                )
                # compute data losses only
                loss, stats_dict = self.fitting_loss.root_fit(observed_data, pred_data)
                log_cur_stats(stats_dict, loss, iter=i)
                loss.backward()
                return loss

            root_optim.step(closure)

        body_pose = self.latent2pose(self.latent_pose)
        stage1_pred_data, _ = self.smpl_results(
            self.trans, self.root_orient, body_pose, self.betas
        )
        per_stage_outputs["stage1"] = stage1_pred_data

        assert stages_res_out is not None
        res_betas = self.betas.clone().detach().cpu().numpy()
        res_trans = self.trans.clone().detach().cpu().numpy()
        res_root_orient = self.root_orient.clone().detach().cpu().numpy()
        res_body_pose = body_pose.clone().detach().cpu().numpy()
        for bidx, res_out_path in enumerate(stages_res_out):
            cur_res_out_path = os.path.join(res_out_path, "stage1_results.npz")
            np.savez(
                cur_res_out_path,
                betas=res_betas[bidx],
                trans=res_trans[bidx],
                root_orient=res_root_orient[bidx],
                pose_body=res_body_pose[bidx],
            )

        #
        # Stage II full pose and shape
        #
        Logger.log(
            "Optimizing stage 2 - full shape and pose for %d iterations.."
            % (num_iter[1])
        )
        self.fitting_loss.set_stage(1)
        self.trans.requires_grad = True
        self.root_orient.requires_grad = True
        self.betas.requires_grad = True
        self.latent_pose.requires_grad = True

        smpl_opt_params = [self.trans, self.root_orient, self.betas, self.latent_pose]

        smpl_optim = torch.optim.LBFGS(
            smpl_opt_params, max_iter=lbfgs_max_iter, lr=lr, line_search_fn=LINE_SEARCH
        )

        for i in range(num_iter[1]):
            Logger.log("ITER: %d" % (i))

            def closure():
                smpl_optim.zero_grad()

                pred_data = dict()
                # Use current params to go through SMPL and get joints3d, verts3d, points3d
                body_pose = self.latent2pose(self.latent_pose)
                pred_data, _ = self.smpl_results(
                    self.trans, self.root_orient, body_pose, self.betas
                )
                pred_data["latent_pose"] = self.latent_pose
                pred_data["betas"] = self.betas
                # compute data losses and pose prior
                loss, stats_dict = self.fitting_loss.smpl_fit(
                    observed_data, pred_data, self.seq_len
                )
                log_cur_stats(stats_dict, loss, iter=i)
                loss.backward()
                return loss

            smpl_optim.step(closure)

        body_pose = self.latent2pose(self.latent_pose)
        stage2_pred_data, _ = self.smpl_results(
            self.trans, self.root_orient, body_pose, self.betas
        )
        per_stage_outputs["stage2"] = stage2_pred_data

        assert stages_res_out is not None
        res_betas = self.betas.clone().detach().cpu().numpy()
        res_trans = self.trans.clone().detach().cpu().numpy()
        res_root_orient = self.root_orient.clone().detach().cpu().numpy()
        res_body_pose = body_pose.clone().detach().cpu().numpy()
        for bidx, res_out_path in enumerate(stages_res_out):
            cur_res_out_path = os.path.join(res_out_path, "stage2_results.npz")
            np.savez(
                cur_res_out_path,
                betas=res_betas[bidx],
                trans=res_trans[bidx],
                root_orient=res_root_orient[bidx],
                pose_body=res_body_pose[bidx],
            )

        assert not self.motion_prior is None
        #
        # Stage III full pose and shape with motion prior
        #
        Logger.log(
            "Optimizing stage 3 - shape and pose with motion prior for %d iterations.."
            % (num_iter[2])
        )
        self.fitting_loss.set_stage(2)
        og_overlap_consist_weight = self.fitting_loss.loss_weights[
            "rgb_overlap_consist"
        ]

        prior_opt_params = []
        # initialize latent motion with inference from the current SMPL sequence
        cur_body_pose = self.latent2pose(self.latent_pose)

        assert not self.optim_floor

        self.latent_motion = self.infer_latent_motion(
            self.trans, self.root_orient, cur_body_pose, self.betas, data_fps
        ).detach()
        self.latent_motion.requires_grad = True

        # also need additional optim params for additional prior inputs at first frame (to enable rollout)
        assert self.motion_prior.model_data_config in [
            "smpl+joints",
            "smpl+joints+contacts",
        ]
        # initialize from current SMPL sequence
        vel_trans = self.trans
        vel_root_orient = self.root_orient
        assert not self.optim_floor
        (
            self.trans_vel,
            self.joints_vel,
            self.root_orient_vel,
        ) = self.estimate_velocities(
            self.trans, self.root_orient, cur_body_pose, self.betas, data_fps
        )

        self.trans_vel = self.trans_vel[:, :1].detach()
        self.joints_vel = self.joints_vel[:, :1].detach()
        self.root_orient_vel = self.root_orient_vel[:, :1].detach()
        self.trans_vel.requires_grad = True
        self.joints_vel.requires_grad = True
        self.root_orient_vel.requires_grad = True
        prior_opt_params = [self.trans_vel, self.joints_vel, self.root_orient_vel]

        # update SMPL optim variables to be only initial state (initialized to current value)
        self.trans = self.trans[:, :1].detach()
        self.root_orient = self.root_orient[:, :1].detach()
        self.latent_pose = self.latent_pose[:, :1].detach()

        self.trans.requires_grad = True
        self.root_orient.requires_grad = True
        self.latent_pose.requires_grad = True
        assert not self.optim_floor
        self.betas.requires_grad = True

        motion_opt_params = [self.trans, self.root_orient, self.latent_pose, self.betas]
        motion_opt_params += [self.latent_motion]
        motion_opt_params += prior_opt_params

        # record intiialization stats
        body_pose = self.latent2pose(self.latent_pose)
        rollout_results, cam_rollout_results = self.rollout_latent_motion(
            self.trans,
            self.root_orient,
            body_pose,
            self.betas,
            prior_opt_params,
            self.latent_motion,
            fit_gender=fit_gender,
        )
        stage3_init_pred_data, _ = self.smpl_results(
            cam_rollout_results["trans"].clone().detach(),
            cam_rollout_results["root_orient"].clone().detach(),
            cam_rollout_results["pose_body"].clone().detach(),
            self.betas,
        )
        if "contacts" in rollout_results:
            stage3_init_pred_data["contacts"] = (
                rollout_results["contacts"].clone().detach()
            )
        per_stage_outputs["stage3_init"] = stage3_init_pred_data

        assert stages_res_out is not None
        res_body_pose = (
            cam_rollout_results["pose_body"].clone().detach().cpu().numpy()
        )
        res_trans = (
            cam_rollout_results["trans"].clone().detach().cpu().cpu().numpy()
        )
        res_root_orient = (
            cam_rollout_results["root_orient"].clone().detach().cpu().numpy()
        )
        res_betas = self.betas.clone().detach().cpu().numpy()

        # camera frame
        for bidx, res_out_path in enumerate(stages_res_out):
            cur_res_out_path = os.path.join(res_out_path, "stage3_init_results.npz")
            save_dict = {
                "betas": res_betas[bidx],
                "trans": res_trans[bidx],
                "root_orient": res_root_orient[bidx],
                "pose_body": res_body_pose[bidx],
            }
            if "contacts" in rollout_results:
                save_dict["contacts"] = (
                    rollout_results["contacts"][bidx].clone().detach().cpu().numpy()
                )
            np.savez(cur_res_out_path, **save_dict)

        assert not self.optim_floor

        init_motion_scale = 1.0  # single-step losses must be scaled commensurately with losses summed over whole sequence
        motion_optim = torch.optim.LBFGS(
            motion_opt_params,
            max_iter=lbfgs_max_iter,
            lr=lr,
            line_search_fn=LINE_SEARCH,
        )

        motion_optim_curr = motion_optim_refine = None
        assert self.stage3_tune_init_state
        freeze_optim_params = [self.latent_motion, self.betas]
        motion_optim_curr = torch.optim.LBFGS(
            freeze_optim_params,
            max_iter=lbfgs_max_iter,
            lr=lr,
            line_search_fn=LINE_SEARCH,
        )
        motion_optim_refine = torch.optim.LBFGS(
            motion_opt_params,
            max_iter=lbfgs_max_iter,
            lr=lr,
            line_search_fn=LINE_SEARCH,
        )
        cur_stage3_nsteps = self.stage3_tune_init_num_frames
        saved_contact_height_weight = self.fitting_loss.loss_weights["contact_height"]
        saved_contact_vel_weight = self.fitting_loss.loss_weights["contact_vel"]
        for i in range(num_iter[2]):
            if (
                self.stage3_tune_init_state
                and i >= self.stage3_tune_init_freeze_start
                and i < self.stage3_tune_init_freeze_end
            ):
                # freeze initial state
                motion_optim = motion_optim_curr
                self.trans.requires_grad = False
                self.root_orient.requires_grad = False
                self.latent_pose.requires_grad = False
                self.trans_vel.requires_grad = False
                self.joints_vel.requires_grad = False
                self.root_orient_vel.requires_grad = False
                if self.stage3_contact_refine_only:
                    self.fitting_loss.loss_weights["contact_height"] = 0.0
                    self.fitting_loss.loss_weights["contact_vel"] = 0.0
                init_motion_scale = (
                    float(self.seq_len) / self.stage3_tune_init_num_frames
                )
            elif self.stage3_tune_init_state and i >= self.stage3_tune_init_freeze_end:
                # refine
                motion_optim = motion_optim_refine
                self.trans.requires_grad = True
                self.root_orient.requires_grad = True
                self.latent_pose.requires_grad = True
                self.trans_vel.requires_grad = True
                self.joints_vel.requires_grad = True
                self.root_orient_vel.requires_grad = True
                self.betas.requires_grad = True
                if self.optim_floor:
                    self.floor_plane.requires_grad = True
                if self.stage3_contact_refine_only:
                    self.fitting_loss.loss_weights[
                        "contact_height"
                    ] = saved_contact_height_weight
                    self.fitting_loss.loss_weights[
                        "contact_vel"
                    ] = saved_contact_vel_weight
                init_motion_scale = (
                    float(self.seq_len) / self.stage3_tune_init_num_frames
                )

            Logger.log("ITER: %d" % (i))

            def closure():
                motion_optim.zero_grad()

                cur_body_pose = self.latent2pose(self.latent_pose)
                assert not self.optim_floor

                pred_data = dict()
                # Use current params to go through SMPL and get joints3d, verts3d, points3d
                cur_trans = self.trans
                cur_root_orient = self.root_orient
                cur_betas = self.betas
                cur_latent_pose = self.latent_pose
                cur_latent_motion = self.latent_motion
                cur_cond_prior = None
                cur_rollout_joints = None
                cur_contacts = cur_contacts_conf = None
                cur_cam_trans = cur_cam_root_orient = None

                if (
                    self.stage3_tune_init_state
                    and i < self.stage3_tune_init_freeze_start
                ):
                    cur_latent_motion = cur_latent_motion[:, : (cur_stage3_nsteps - 1)]
                # rollout full sequence with current latent dynamics
                # rollout_results are in prior space, cam_rollout_results are in camera frame
                rollout_results, cam_rollout_results = self.rollout_latent_motion(
                    cur_trans,
                    cur_root_orient,
                    cur_body_pose,
                    cur_betas,
                    prior_opt_params,
                    cur_latent_motion,
                    return_prior=self.cond_prior,
                    fit_gender=fit_gender,
                )
                cur_trans = rollout_results["trans"]
                cur_root_orient = rollout_results["root_orient"]
                cur_body_pose = rollout_results["pose_body"]
                cur_cam_trans = cam_rollout_results["trans"]
                cur_cam_root_orient = cam_rollout_results["root_orient"]
                if self.cond_prior:
                    cur_cond_prior = rollout_results["cond_prior"]
                # re-encode entire body pose sequence
                cur_latent_pose = self.pose2latent(cur_body_pose)
                cur_rollout_joints = rollout_results["joints"]
                if "contacts" in rollout_results:
                    cur_contacts = rollout_results["contacts"]
                    cur_contacts_conf = rollout_results["contacts_conf"]

                pred_data, _ = self.smpl_results(
                    cur_trans, cur_root_orient, cur_body_pose, cur_betas
                )

                pred_data["latent_pose"] = cur_latent_pose
                pred_data["betas"] = cur_betas

                pred_data["latent_motion"] = cur_latent_motion
                # info for init state pose prior
                pred_data["joints_vel"] = self.joints_vel
                pred_data["trans_vel"] = self.trans_vel
                pred_data["root_orient_vel"] = self.root_orient_vel
                pred_data["joints3d_rollout"] = cur_rollout_joints
                if cur_contacts is not None:
                    pred_data["contacts"] = cur_contacts
                if cur_contacts_conf is not None:
                    pred_data["contacts_conf"] = cur_contacts_conf

                cam_pred_data = pred_data
                assert not self.optim_floor

                loss_nsteps = self.seq_len
                loss_obs_data = observed_data
                if (
                    self.stage3_tune_init_state
                    and i < self.stage3_tune_init_freeze_start
                ):
                    loss_obs_data = {
                        k: v[:, :cur_stage3_nsteps]
                        for k, v in observed_data.items()
                        if k != "prev_batch_overlap_res"
                    }
                    if "prev_batch_overlap_res" in observed_data:
                        loss_obs_data["prev_batch_overlap_res"] = observed_data[
                            "prev_batch_overlap_res"
                        ]
                    loss_nsteps = cur_stage3_nsteps
                    # if in initial stage, don't want to use overlap constraints
                    self.fitting_loss.loss_weights["rgb_overlap_consist"] = 0.0

                # compute data losses, pose & motion prior
                loss, stats_dict = self.fitting_loss.motion_fit(
                    loss_obs_data,
                    pred_data,
                    cam_pred_data,
                    loss_nsteps,
                    cond_prior=cur_cond_prior,
                    init_motion_scale=init_motion_scale,
                )

                if (
                    self.stage3_tune_init_state
                    and i < self.stage3_tune_init_freeze_start
                ):
                    # change it back
                    self.fitting_loss.loss_weights[
                        "rgb_overlap_consist"
                    ] = og_overlap_consist_weight

                log_cur_stats(stats_dict, loss, iter=i)
                loss.backward()
                return loss

            motion_optim.step(closure)

        body_pose = self.latent2pose(self.latent_pose)
        rollout_joints = rollout_results = None

        # rollout and reset self.smpl_params to rolled out results so that get_optim_result works
        rollout_results, cam_rollout_results = self.rollout_latent_motion(
            self.trans,
            self.root_orient,
            body_pose,
            self.betas,
            prior_opt_params,
            self.latent_motion,
            fit_gender=fit_gender,
        )
        body_pose = rollout_results["pose_body"]
        self.latent_pose = self.pose2latent(body_pose)
        self.trans = cam_rollout_results["trans"]
        self.root_orient = cam_rollout_results["root_orient"]
        rollout_joints = rollout_results["joints"]

        stage3_pred_data, _ = self.smpl_results(
            self.trans, self.root_orient, body_pose, self.betas
        )
        if rollout_joints is not None:
            if self.optim_floor:
                stage3_pred_data["prior_joints3d_rollout"] = rollout_joints
            else:
                stage3_pred_data["joints3d_rollout"] = rollout_joints
        if rollout_results is not None and "contacts" in rollout_results:
            stage3_pred_data["contacts"] = rollout_results["contacts"]
        per_stage_outputs["stage3"] = stage3_pred_data

        final_optim_res = self.get_optim_result(body_pose)
        if rollout_results is not None and "contacts" in rollout_results:
            final_optim_res["contacts"] = rollout_results["contacts"]

        assert not self.optim_floor

        return final_optim_res, per_stage_outputs

    def estimate_velocities(
        self, trans, root_orient, body_pose, betas, data_fps, smpl_results=None
    ):
        """
        From the SMPL sequence, estimates velocity inputs to the motion prior.

        - trans : root translation
        - root_orient : aa root orientation
        - body_pose
        """
        B, T, _ = trans.size()
        h = 1.0 / data_fps
        assert self.motion_prior.model_data_config in [
            "smpl+joints",
            "smpl+joints+contacts",
        ]
        if smpl_results is None:
            smpl_results, _ = self.smpl_results(
                trans, root_orient, body_pose, betas
            )
        trans_vel = self.estimate_linear_velocity(trans, h)
        joints_vel = self.estimate_linear_velocity(smpl_results["joints3d"], h)
        root_orient_mat = batch_rodrigues(root_orient.reshape((-1, 3))).reshape(
            (B, T, 3, 3)
        )
        root_orient_vel = self.estimate_angular_velocity(root_orient_mat, h)
        return trans_vel, joints_vel, root_orient_vel

    def estimate_linear_velocity(self, data_seq, h):
        """
        Given some batched data sequences of T timesteps in the shape (B, T, ...), estimates
        the velocity for the middle T-2 steps using a second order central difference scheme.
        The first and last frames are with forward and backward first-order
        differences, respectively
        - h : step size
        """
        # first steps is forward diff (t+1 - t) / h
        init_vel = (data_seq[:, 1:2] - data_seq[:, :1]) / h
        # middle steps are second order (t+1 - t-1) / 2h
        middle_vel = (data_seq[:, 2:] - data_seq[:, 0:-2]) / (2 * h)
        # last step is backward diff (t - t-1) / h
        final_vel = (data_seq[:, -1:] - data_seq[:, -2:-1]) / h

        vel_seq = torch.cat([init_vel, middle_vel, final_vel], dim=1)
        return vel_seq

    def estimate_angular_velocity(self, rot_seq, h):
        """
        Given a batch of sequences of T rotation matrices, estimates angular velocity at T-2 steps.
        Input sequence should be of shape (B, T, ..., 3, 3)
        """
        # see https://en.wikipedia.org/wiki/Angular_velocity#Calculation_from_the_orientation_matrix
        dRdt = self.estimate_linear_velocity(rot_seq, h)
        R = rot_seq
        RT = R.transpose(-1, -2)
        # compute skew-symmetric angular velocity tensor
        w_mat = torch.matmul(dRdt, RT)
        # pull out angular velocity vector
        # average symmetric entries
        w_x = (-w_mat[..., 1, 2] + w_mat[..., 2, 1]) / 2.0
        w_y = (w_mat[..., 0, 2] - w_mat[..., 2, 0]) / 2.0
        w_z = (-w_mat[..., 0, 1] + w_mat[..., 1, 0]) / 2.0
        w = torch.stack([w_x, w_y, w_z], axis=-1)
        return w

    def infer_latent_motion(
        self, trans, root_orient, body_pose, betas, data_fps, full_forward_pass=False
    ):
        """
        By default, gets a sequence of z's from the current SMPL optim params.

        If full_forward_pass is true, in addition to inference, also samples
        from the posterior and feeds through the motion prior decoder to get
        all terms needed to calculate the ELBO.
        """
        B, T, _ = trans.size()
        h = 1.0 / data_fps
        latent_motion = None
        assert self.motion_prior.model_data_config in [
            "smpl+joints",
            "smpl+joints+contacts",
        ]
        assert not self.optim_floor

        smpl_results, _ = self.smpl_results(trans, root_orient, body_pose, betas)
        trans_vel, joints_vel, root_orient_vel = self.estimate_velocities(
            trans,
            root_orient,
            body_pose,
            betas,
            data_fps,
            smpl_results=smpl_results,
        )

        joints = smpl_results["joints3d"]

        # convert rots
        # body pose and root orient are both in aa
        root_orient_in = root_orient
        body_pose_in = body_pose
        assert (
            self.motion_prior.in_rot_rep == "mat"
            or self.motion_prior.in_rot_rep == "6d"
        )
        root_orient_in = batch_rodrigues(root_orient.reshape(-1, 3)).reshape(
            (B, T, 9)
        )
        body_pose_in = batch_rodrigues(body_pose.reshape(-1, 3)).reshape(
            (B, T, J_BODY * 9)
        )
        assert not self.motion_prior.in_rot_rep == "6d"
        joints_in = joints.reshape((B, T, -1))
        joints_vel_in = joints_vel.reshape((B, T, -1))

        seq_dict = {
            "trans": trans,
            "trans_vel": trans_vel,
            "root_orient": root_orient_in,
            "root_orient_vel": root_orient_vel,
            "pose_body": body_pose_in,
            "joints": joints_in,
            "joints_vel": joints_vel_in,
        }

        infer_results = self.motion_prior.infer_global_seq(
            seq_dict, full_forward_pass=full_forward_pass
        )
        assert not full_forward_pass
        prior_z, posterior_z = infer_results
        infer_results = posterior_z[0]  # mean of the approximate posterior

        return infer_results

    def rollout_latent_motion(
        self,
        trans,
        root_orient,
        body_pose,
        betas,
        prior_opt_params,
        latent_motion,
        return_prior=False,
        return_vel=False,
        fit_gender="neutral",
        use_mean=False,
        num_steps=-1,
        canonicalize_input=False,
    ):
        """
        Given initial state SMPL parameters and additional prior inputs, rolls out the sequence
        using the encoded latent motion and the motion prior to obtain a full SMPL sequence.

        If latent_motion is None, instead samples num_steps into the future sequence from the prior. if use_mean does this
        using the mean of the prior rather than random samples.

        If canonicalize_input is True, the given initial state is first transformed into the local canonical
        frame before roll out
        """
        B = trans.size(0)
        is_sampling = latent_motion is None
        Tm1 = num_steps if latent_motion is None else latent_motion.size(1)
        if is_sampling and Tm1 <= 0:
            Logger.log("num_steps must be positive to sample!")
            exit()

        cam_trans = trans
        cam_root_orient = root_orient
        assert not self.optim_floor

        x_past = joints = None
        trans_vel = joints_vel = root_orient_vel = None
        rollout_in_dict = dict()
        assert self.motion_prior.model_data_config in [
            "smpl+joints",
            "smpl+joints+contacts",
        ]
        trans_vel, joints_vel, root_orient_vel = prior_opt_params
        smpl_results, _ = self.smpl_results(trans, root_orient, body_pose, betas)
        joints = smpl_results["joints3d"]
        # update to correct rotations for input
        root_orient_in = root_orient
        body_pose_in = body_pose
        if (
            self.motion_prior.in_rot_rep == "mat"
            or self.motion_prior.in_rot_rep == "6d"
        ):
            root_orient_in = batch_rodrigues(root_orient.reshape(-1, 3)).reshape(
                (B, 1, 9)
            )
            body_pose_in = batch_rodrigues(body_pose.reshape(-1, 3)).reshape(
                (B, 1, J_BODY * 9)
            )
        if self.motion_prior.in_rot_rep == "6d":
            root_orient_in = root_orient_in[:, :, :6]
            body_pose_in = body_pose_in.reshape((B, 1, J_BODY, 9))[
                :, :, :, :6
            ].reshape((B, 1, J_BODY * 6))
        joints_in = joints.reshape((B, 1, -1))
        joints_vel_in = joints_vel.reshape((B, 1, -1))

        rollout_in_dict = {
            "trans": trans,
            "trans_vel": trans_vel,
            "root_orient": root_orient_in,
            "root_orient_vel": root_orient_vel,
            "pose_body": body_pose_in,
            "joints": joints_in,
            "joints_vel": joints_vel_in,
        }

        roll_output = self.motion_prior.roll_out(
            None,
            rollout_in_dict,
            Tm1,
            z_seq=latent_motion,
            return_prior=return_prior,
            return_z=is_sampling,
            canonicalize_input=canonicalize_input,
            gender=[fit_gender] * B,
            betas=betas.reshape((B, 1, -1)),
        )

        pred_dict = prior_out = None
        if return_prior:
            pred_dict, prior_out = roll_output
        else:
            pred_dict = roll_output

        out_dict = dict()
        if self.motion_prior.model_data_config in [
            "smpl+joints",
            "smpl+joints+contacts",
        ]:
            # copy what we need in correct format and concat with initial state
            trans_out = torch.cat([trans, pred_dict["trans"]], dim=1)
            root_orient_out = pred_dict["root_orient"]
            root_orient_out = rotation_matrix_to_angle_axis(
                root_orient_out.reshape((-1, 3, 3))
            ).reshape((B, Tm1, 3))
            root_orient_out = torch.cat([root_orient, root_orient_out], dim=1)
            body_pose_out = pred_dict["pose_body"]
            body_pose_out = rotation_matrix_to_angle_axis(
                body_pose_out.reshape((-1, 3, 3))
            ).reshape((B, Tm1, J_BODY * 3))
            body_pose_out = torch.cat([body_pose, body_pose_out], dim=1)
            joints_out = torch.cat(
                [joints, pred_dict["joints"].reshape((B, Tm1, -1, 3))], dim=1
            )
            out_dict = {
                "trans": trans_out,
                "root_orient": root_orient_out,
                "pose_body": body_pose_out,
                "joints": joints_out,
            }
            if return_vel:
                trans_vel_out = torch.cat([trans_vel, pred_dict["trans_vel"]], dim=1)
                out_dict["trans_vel"] = trans_vel_out
                root_orient_vel_out = torch.cat(
                    [root_orient_vel, pred_dict["root_orient_vel"]], dim=1
                )
                out_dict["root_orient_vel"] = root_orient_vel_out
                joints_vel_out = torch.cat(
                    [joints_vel, pred_dict["joints_vel"].reshape((B, Tm1, -1, 3))],
                    dim=1,
                )
                out_dict["joints_vel"] = joints_vel_out
            if return_prior:
                out_dict["cond_prior"] = prior_out
            if self.motion_prior.model_data_config == "smpl+joints+contacts":
                pred_contacts = pred_dict["contacts"]
                # get binary classification
                contact_conf = torch.sigmoid(pred_contacts)
                pred_contacts = (contact_conf > CONTACT_THRESH).to(torch.float)
                # expand to full body
                full_contact_conf = torch.zeros((B, Tm1, len(SMPL_JOINTS))).to(
                    contact_conf
                )
                full_contact_conf[:, :, CONTACT_INDS] = (
                    full_contact_conf[:, :, CONTACT_INDS] + contact_conf
                )
                full_contacts = torch.zeros((B, Tm1, len(SMPL_JOINTS))).to(
                    pred_contacts
                )
                full_contacts[:, :, CONTACT_INDS] = (
                    full_contacts[:, :, CONTACT_INDS] + pred_contacts
                )
                # repeat first entry for t0
                full_contact_conf = torch.cat(
                    [full_contact_conf[:, 0:1], full_contact_conf], dim=1
                )
                full_contacts = torch.cat([full_contacts[:, 0:1], full_contacts], dim=1)
                # print(full_contacts.size())
                # print(full_contacts)
                out_dict["contacts_conf"] = full_contact_conf
                out_dict["contacts"] = full_contacts
            if is_sampling:
                out_dict["z"] = pred_dict["z"]
        else:
            raise NotImplementedError(
                "Only smpl+joints motion prior configuration is supported!"
            )

        cam_dict = dict()
        if self.optim_floor:
            # also must return trans and root orient in camera frame
            data_dict = {
                "trans": out_dict["trans"],
                "root_orient": out_dict["root_orient"],
            }
            cam_dict = self.apply_cam2prior(
                data_dict,
                self.cam2prior_R,
                self.cam2prior_t,
                self.cam2prior_root_height,
                out_dict["pose_body"],
                betas,
                self.init_fidx,
                inverse=True,
            )
        else:
            # camera and prior frame are the same if not optimizing floor
            cam_dict["trans"] = out_dict["trans"]
            cam_dict["root_orient"] = out_dict["root_orient"]
        cam_dict["pose_body"] = out_dict["pose_body"]  # same for both

        return out_dict, cam_dict

    def get_optim_result(self, body_pose=None):
        """
        Collect final outputs into a dict.
        """
        if body_pose is None:
            body_pose = self.latent2pose(self.latent_pose)
        optim_result = {
            "trans": self.trans.clone().detach(),
            "root_orient": self.root_orient.clone().detach(),
            "pose_body": body_pose.clone().detach(),
            "betas": self.betas.clone().detach(),
            "latent_pose": self.latent_pose.clone().detach(),
        }
        optim_result["latent_motion"] = self.latent_motion.clone().detach()
        if self.optim_floor:
            ground_plane = parse_floor_plane(self.floor_plane)
            optim_result["floor_plane"] = ground_plane.clone().detach()

        return optim_result

    def latent2pose(self, latent_pose):
        """
        Converts VPoser latent embedding to aa body pose.
        latent_pose : B x T x D
        body_pose : B x T x J*3
        """
        B, T, _ = latent_pose.size()
        latent_pose = latent_pose.reshape((-1, self.latent_pose_dim))
        body_pose = self.pose_prior.decode(latent_pose, output_type="matrot")
        body_pose = rotation_matrix_to_angle_axis(
            body_pose.reshape((B * T * J_BODY, 3, 3))
        ).reshape((B, T, J_BODY * 3))
        return body_pose

    def pose2latent(self, body_pose):
        """
        Encodes aa body pose to VPoser latent space.
        body_pose : B x T x J*3
        latent_pose : B x T x D
        """
        B, T, _ = body_pose.size()
        body_pose = body_pose.reshape((-1, J_BODY * 3))
        latent_pose_distrib = self.pose_prior.encode(body_pose)
        latent_pose = latent_pose_distrib.mean.reshape((B, T, self.latent_pose_dim))
        return latent_pose

    def smpl_results(self, trans, root_orient, body_pose, beta):
        """
        Forward pass of the SMPL model and populates pred_data accordingly with
        joints3d, verts3d, points3d.

        trans : B x T x 3
        root_orient : B x T x 3
        body_pose : B x T x J*3
        beta : B x D
        """
        B, T, _ = trans.size()
        if T == 1:
            # must expand to use with body model
            trans = trans.expand((self.batch_size, self.seq_len, 3))
            root_orient = root_orient.expand((self.batch_size, self.seq_len, 3))
            body_pose = body_pose.expand((self.batch_size, self.seq_len, J_BODY * 3))
        elif T != self.seq_len:
            # raise NotImplementedError('Only supports single or all steps in body model.')
            pad_size = self.seq_len - T
            trans, root_orient, body_pose = self.zero_pad_tensors(
                [trans, root_orient, body_pose], pad_size
            )

        betas = beta.reshape((self.batch_size, 1, self.num_betas)).expand(
            (self.batch_size, self.seq_len, self.num_betas)
        )
        smpl_body = self.body_model(
            pose_body=body_pose.reshape((self.batch_size * self.seq_len, -1)),
            pose_hand=None,
            betas=betas.reshape((self.batch_size * self.seq_len, -1)),
            root_orient=root_orient.reshape((self.batch_size * self.seq_len, -1)),
            trans=trans.reshape((self.batch_size * self.seq_len, -1)),
        )
        # body joints
        joints3d = smpl_body.Jtr.reshape((self.batch_size, self.seq_len, -1, 3))[:, :T]
        body_joints3d = joints3d[:, :, : len(SMPL_JOINTS), :]
        added_joints3d = joints3d[:, :, len(SMPL_JOINTS) :, :]
        # ALL body vertices
        points3d = smpl_body.v.reshape((self.batch_size, self.seq_len, -1, 3))[:, :T]
        # SELECT body vertices
        verts3d = points3d[:, :T, KEYPT_VERTS, :]

        pred_data = {
            "joints3d": body_joints3d,
            "points3d": points3d,
            "verts3d": verts3d,
            "joints3d_extra": added_joints3d,  # hands and selected OP vertices (if applicable)
            "faces": smpl_body.f,  # always the same, but need it for some losses
        }

        return pred_data, smpl_body

    def zero_pad_tensors(self, pad_list, pad_size):
        """
        Assumes tensors in pad_list are B x T x D and pad temporal dimension
        """
        B = pad_list[0].size(0)
        new_pad_list = []
        for pad_idx, pad_tensor in enumerate(pad_list):
            padding = torch.zeros((B, pad_size, pad_tensor.size(2))).to(pad_tensor)
            new_pad_list.append(torch.cat([pad_tensor, padding], dim=1))
        return new_pad_list
