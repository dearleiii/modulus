# ignore_header_test
# ruff: noqa: E402

# © Copyright 2023 HP Development Company, L.P.
# SPDX-FileCopyrightText: Copyright (c) 2023 - 2024 NVIDIA CORPORATION & AFFILIATES.
# SPDX-FileCopyrightText: All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


import os, json
from absl import app
import random
import time
from tqdm import tqdm
import numpy as np
import math
import ast

try:
    import tensorflow as tf
except ImportError:
    raise ImportError(
        "Mesh Graph Net Datapipe requires the Tensorflow library. Install the "
        + "package at: https://www.tensorflow.org/install"
    )

physical_devices = tf.config.list_physical_devices("GPU")
try:
    for device_ in physical_devices:
        tf.config.experimental.set_memory_growth(device_, True)
except:
    # Invalid device or cannot modify virtual devices once initialized.
    pass

import torch
from torch.utils.tensorboard import SummaryWriter

from modulus.models.vfgn.graph_network_modules import LearnedSimulator
from modulus.datapipes.vfgn import utils
from modulus.datapipes.vfgn.utils import _read_metadata
from modulus.models.vfgn.graph_dataset import GraphDataset

from modulus.distributed.manager import DistributedManager
from modulus.launch.logging import (
    LaunchLogger,
    PythonLogger,
    initialize_wandb,
    initialize_mlflow,
    RankZeroLoggingWrapper,
)

from constants import Constants

C = Constants()


class Stats:
    """
    Represents statistical attributes with methods for device transfer.
    """

    def __init__(self, mean, std):
        self.mean = mean
        self.std = std

    def to(self, device):
        """Transfers the mean and standard deviation to a specified device."""
        self.mean = self.mean.to(device)
        self.std = self.std.to(device)
        return self


device = "cpu"

INPUT_SEQUENCE_LENGTH = 5  # calculate the last 5 velocities. [options: 5, 10]
PREDICT_LENGTH = 1  # [options: 5]

NUM_PARTICLE_TYPES = 3
KINEMATIC_PARTICLE_ID = 0  # refers to anchor point
METAL_PARTICLE_ID = 2  # refers to normal particles
ANCHOR_PLANE_PARTICLE_ID = 1  # refers to anchor plane


cast = lambda v: np.array(v, dtype=np.float64)


def Inference(rank_zero_logger, dist):
    """
    Executes the testing phase for a graph-based model, generating and
    storing predictions.
    """
    rank_zero_logger.info(
        f"\n\n.......... Start calling model inference with defined data path ........\n\n"
    )

    # config test dataset
    dataset = GraphDataset(
        # size=C.num_steps,
        mode="rollout",
        split=C.eval_split,
        data_path=C.data_path,
        batch_size=C.batch_size,
    )
    rank_zero_logger.info(
        f"Initialized inference dataset with mode {dataset.mode}, dataset size {dataset.size}..."
    )

    metadata = _read_metadata(C.data_path)
    acceleration_stats = Stats(
        torch.DoubleTensor(cast(metadata["acc_mean"])),
        torch.DoubleTensor(utils._combine_std(cast(metadata["acc_std"]), C.noise_std)),
    )
    velocity_stats = Stats(
        torch.DoubleTensor(cast(metadata["vel_mean"])),
        torch.DoubleTensor(utils._combine_std(cast(metadata["vel_std"]), C.noise_std)),
    )
    context_stats = Stats(
        torch.DoubleTensor(cast(metadata["context_mean"])),
        torch.DoubleTensor(
            utils._combine_std(cast(metadata["context_std"]), C.noise_std)
        ),
    )

    normalization_stats = {
        "acceleration": acceleration_stats,
        "velocity": velocity_stats,
        "context": context_stats,
    }

    model = LearnedSimulator(
        num_dimensions=metadata["dim"] * PREDICT_LENGTH,
        num_seq=INPUT_SEQUENCE_LENGTH,
        boundaries=torch.DoubleTensor(metadata["bounds"]),
        num_particle_types=NUM_PARTICLE_TYPES,
        particle_type_embedding_size=16,
        normalization_stats=normalization_stats,
    )
    rank_zero_logger.info(f"Initialized model with LearnedSimulator")

    loaded = False
    example_index = 0
    device = "cpu"
    with torch.no_grad():
        for features, targets in tqdm(dataset):
            if loaded is False:
                # input feature size is dynamic, so need to forward model in CPU before loading into GPU
                global_context = features["step_context"].to(device)
                if global_context is None:
                    global_context_step = None
                else:
                    global_context_step = global_context[:-1]
                    global_context_step = torch.reshape(global_context_step, [1, -1])

                model.inference(
                    position_sequence=features["position"][
                        :, 0:INPUT_SEQUENCE_LENGTH
                    ].to(device),
                    n_particles_per_example=features["n_particles_per_example"].to(
                        device
                    ),
                    n_edges_per_example=features["n_edges_per_example"].to(device),
                    senders=features["senders"].to(device),
                    receivers=features["receivers"].to(device),
                    predict_length=PREDICT_LENGTH,
                    particle_types=features["particle_type"].to(device),
                    global_context=global_context_step.to(device),
                )

                # Loading the pretrained model from model ckpt_path_vfgn
                # For provided ckpt with missing keys, ignore
                model.load_state_dict(torch.load(C.ckpt_path_vfgn), strict=False)
                device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
                rank_zero_logger.info(f"Device: {device}")
                rank_zero_logger.info(
                    f"Loaded model from ckpt path: {C.ckpt_path_vfgn}"
                )

                # config optimizer
                # todo: check msg passing device
                model.setMessagePassingDevices(["cuda:0"])
                model = model.to(device)
                model.eval()
                loaded = True

            initial_positions = features["position"][:, :INPUT_SEQUENCE_LENGTH].to(
                device
            )

            global_context = features["step_context"].to(device)
            rank_zero_logger.info(
                f"\n Read global_context shape:  {global_context.shape}"
            )

            rank_zero_logger.info(
                f"\n Initial_positions shape: {initial_positions.shape}"
            )

            # num_steps = ground_truth_positions.shape[1]
            num_steps = global_context.shape[0] - INPUT_SEQUENCE_LENGTH
            rank_zero_logger.info(f"\n Start prediction for {num_steps} steps...... ")

            current_positions = initial_positions
            updated_predictions = []

            start_time = time.time()
            rank_zero_logger.info(f"Start time: {start_time}\n")

            for step in range(num_steps):
                rank_zero_logger.info(f"start predictiong step: {step}")
                if global_context is None:
                    global_context_step = None
                    rank_zero_logger.info(f"global_context_step is None")
                else:
                    read_step_context = global_context[: step + INPUT_SEQUENCE_LENGTH]
                    zero_pad = torch.zeros(
                        [global_context.shape[0] - read_step_context.shape[0] - 1, 1],
                        dtype=features["step_context"].dtype,
                    ).to(device)

                    global_context_step = torch.cat([read_step_context, zero_pad], 0)
                    global_context_step = torch.reshape(global_context_step, [1, -1])

                predict_positions = model.inference(
                    position_sequence=current_positions.to(device),
                    n_particles_per_example=features["n_particles_per_example"].to(
                        device
                    ),
                    n_edges_per_example=features["n_edges_per_example"].to(device),
                    senders=features["senders"].to(device),
                    receivers=features["receivers"].to(device),
                    predict_length=PREDICT_LENGTH,
                    particle_types=features["particle_type"].to(device),
                    global_context=global_context_step.to(device),
                )

                kinematic_mask = (
                    utils.get_kinematic_mask(features["particle_type"])
                    .to(torch.bool)
                    .to(device)
                )

                predict_positions = predict_positions[:, 0].squeeze(1)

                # todo: implement the masking for predicted results for different particle types
                # kinematic_mask = torch.repeat_interleave(
                #     kinematic_mask, repeats=predict_positions.shape[-1]
                # )
                # kinematic_mask = torch.reshape(
                #     kinematic_mask, [-1, predict_positions.shape[-1]]
                # )
                # next_position = torch.where(
                #     kinematic_mask, positions_ground_truth, predict_positions
                # )
                next_position = predict_positions

                updated_predictions.append(next_position)
                current_positions = torch.cat(
                    [current_positions[:, 1:], next_position.unsqueeze(1)], axis=1
                )

            updated_predictions = torch.stack(updated_predictions)
            rank_zero_logger.info(
                f"\n\n finished running all stages, initial_positions shape: {initial_positions.shape},\n"
                f"\n updated_predictions shape: {updated_predictions.shape}"
            )

            initial_positions_list = initial_positions.cpu().numpy().tolist()
            updated_predictions_list = updated_predictions.cpu().numpy().tolist()
            particle_types_list = features["particle_type"].cpu().numpy().tolist()
            global_context_list = global_context.cpu().numpy().tolist()

            rollout_op = {
                "initial_positions": initial_positions_list,
                "predicted_rollout": updated_predictions_list,
                "particle_types": particle_types_list,
                "global_context": global_context_list,
            }

            # Add a leading axis, since Estimator's predict method insists that all
            # tensors have a shared leading batch axis fo the same dims.
            # rollout_op = tree.map_structure(lambda x: x.numpy(), rollout_op)

            rollout_op["metadata"] = metadata
            filename = f"rollout_{C.eval_split}_{example_index}.json"
            filename = os.path.join(C.output_path, filename)
            if not os.path.exists(C.output_path):
                os.makedirs(C.output_path)
            with open(filename, "w") as file_object:
                json.dump(rollout_op, file_object)

            example_index += 1
            rank_zero_logger.info(f"prediction time: {time.time()-start_time}\n")


def main(_):
    """
    Triggers the train or test phase based on the configuration.
    """
    # initialize distributed manager
    DistributedManager.initialize()
    dist = DistributedManager()

    rank_zero_logger = RankZeroLoggingWrapper(logger, dist)  # Rank 0 logger

    if C.mode == "rollout":
        Inference(rank_zero_logger, dist)
    else:
        raise NotImplementedError("Mode not implemented ")


if __name__ == "__main__":
    # tf.disable_v2_behavior()
    LaunchLogger.initialize()  # Modulus launch logger
    logger = PythonLogger("main")  # General python logger
    logger.file_logging()

    app.run(main)