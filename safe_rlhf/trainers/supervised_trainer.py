# Copyright 2023 PKU-Alignment Team. All Rights Reserved.
# Copyright 2023 Javier Rando (ETH Zurich). All Rights Reserved.
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
# ==============================================================================
"""Trainer base class for supervised training."""

from __future__ import annotations

import abc
import argparse
from typing import Any, ClassVar

import deepspeed
import torch
import torch.distributed as dist
from deepspeed.ops.adam import FusedAdam
from torch.utils.data import DataLoader
from torch.utils.data.distributed import DistributedSampler
from tqdm import tqdm
from transformers import AutoModelForCausalLM, get_scheduler
from transformers.deepspeed import HfDeepSpeedConfig

from safe_rlhf.configs import ADAM_BETAS
from safe_rlhf.datasets import TokenizedDataset
from safe_rlhf.models import load_pretrained_models
from safe_rlhf.trainers.base import TrainerBase
from safe_rlhf.utils import get_optimizer_grouped_parameters, is_main_process, to_device


class SupervisedTrainer(TrainerBase):
    """Trainer base class for supervised training.

    Abstract methods:
        loss: Compute supervised training loss.
        train_step: Perform a single training step.
    """

    TRAINING_TYPE: ClassVar[str] = "supervised"
    DATASET_TYPE: ClassVar[type[TokenizedDataset]]
    MODEL_TYPE = AutoModelForCausalLM

    model: deepspeed.DeepSpeedEngine
    ds_config: dict[str, Any]

    def __init__(self, args: argparse.Namespace, ds_config: dict[str, Any]) -> None:
        """Initialize trainer."""
        self.args = args
        self.ds_config = ds_config

        self.init_models()
        dist.barrier()
        self.init_datasets()
        dist.barrier()
        self.init_engines()
        dist.barrier()
        self.init_logger()

    def init_models(self) -> None:
        """Initialize model and tokenizer."""
        if (
            self.ds_config is not None
            and self.ds_config["zero_optimization"]["stage"] == 3
        ):
            self.dstchf = HfDeepSpeedConfig(self.ds_config)

        self.model, self.tokenizer = load_pretrained_models(
            self.args.model_name_or_path,
            model_max_length=self.args.max_length,
            padding_side="right",
            auto_model_type=self.MODEL_TYPE,
            trust_remote_code=self.args.trust_remote_code,
            add_eos_token=True,
        )

    def init_datasets(self) -> None:
        """Initialize training and evaluation datasets."""
        train_dataset = self.DATASET_TYPE(
            self.args.train_datasets,
            tokenizer=self.tokenizer,
        )

        if self.args.need_eval:
            if (
                self.args.eval_datasets is None
                and self.args.eval_split_ratio is not None
            ):
                train_dataset, eval_dataset = train_dataset.split_train_test(
                    split_ratio=self.args.eval_split_ratio,
                )
            elif (
                self.args.eval_datasets is not None
                and self.args.eval_split_ratio is None
            ):
                eval_dataset = self.DATASET_TYPE(
                    self.args.eval_datasets,
                    tokenizer=self.tokenizer,
                )
            else:
                raise ValueError(
                    "Either `eval_datasets` or `eval_split_ratio` should be provided."
                )

            self.eval_dataloader = DataLoader(
                eval_dataset,
                collate_fn=eval_dataset.get_collator(),
                sampler=DistributedSampler(eval_dataset, shuffle=True),
                batch_size=self.args.per_device_eval_batch_size,
            )
        else:
            self.eval_dataloader = None

        self.train_dataloader = DataLoader(
            train_dataset,
            collate_fn=train_dataset.get_collator(),
            sampler=DistributedSampler(train_dataset, shuffle=True),
            batch_size=self.args.per_device_train_batch_size,
        )

        # Print one example
        if is_main_process():
            for batch in self.train_dataloader:
                for key in batch.keys():
                    print(f"{key}: {batch[key][0]}")
                    print(
                        self.tokenizer.decode(
                            torch.where(batch[key][0] <= 0, 1, batch[key][0])
                        )
                    )
                break

    def init_engines(self) -> None:
        """Initialize DeepSpeed engines."""
        self.args.num_update_steps_per_epoch = (
            len(self.train_dataloader) + self.args.gradient_accumulation_steps - 1
        ) // self.args.gradient_accumulation_steps
        self.args.total_training_steps = (
            self.args.epochs * self.args.num_update_steps_per_epoch
        )

        optimizer_grouped_parameters = get_optimizer_grouped_parameters(
            self.model,
            self.args.weight_decay,
        )

        optimizer = FusedAdam(
            optimizer_grouped_parameters,
            lr=self.args.learning_rate,
            betas=ADAM_BETAS,
        )

        lr_scheduler = get_scheduler(
            name=self.args.lr_scheduler_type,
            optimizer=optimizer,
            num_warmup_steps=self.args.num_warmup_steps,
            num_training_steps=self.args.total_training_steps,
        )

        self.model, *_ = deepspeed.initialize(
            model=self.model,
            optimizer=optimizer,
            args=self.args,
            config=self.ds_config,
            lr_scheduler=lr_scheduler,
            dist_init_required=True,
        )

        if self.args.gradient_checkpointing:
            self.model.gradient_checkpointing_enable()

    @abc.abstractmethod
    def loss(self, *args: Any, **kwargs: Any) -> dict[str, torch.Tensor]:
        """Compute supervised training loss."""
        raise NotImplementedError

    @abc.abstractmethod
    def train_step(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """Perform a single training step."""
        raise NotImplementedError

    def train(self) -> None:
        """Train the model."""
        self.logger.print("***** Running training *****")

        progress_bar = tqdm(
            total=self.args.epochs * len(self.train_dataloader),
            desc=f"Training 1/{self.args.epochs} epoch",
            position=0,
            leave=True,
            disable=not is_main_process(),
        )

        if self.args.need_eval:
            self.logger.print("\n***** Evaluating at the beginning *****")
            self.logger.log(self.eval(), step=0)

        for epoch in range(self.args.epochs):
            self.model.train()

            for step, batch in enumerate(self.train_dataloader):
                global_step = epoch * len(self.train_dataloader) + step + 1
                info = self.train_step(**to_device(batch, self.args.device))
                info["train/epoch"] = global_step / len(self.train_dataloader)
                self.logger.log(info, step=global_step)

                progress_bar.set_description(
                    f'Training {epoch + 1}/{self.args.epochs} epoch (loss {info["train/loss"]:.4f})',
                )
                progress_bar.update()

                if global_step % self.args.save_interval == 0:
                    self.logger.print(f"Saving checkpoint at step {global_step} ...")
                    self.model.save_checkpoint(self.args.output_dir, tag=global_step)
                    self.logger.print("Checkpoint saved.")

                if (
                    self.args.need_eval
                    and self.args.eval_strategy == "steps"
                    and global_step % self.args.eval_interval == 0
                ):
                    self.logger.print(f"\n***** Evaluating at step {global_step} *****")
                    self.logger.log(self.eval(), step=global_step)

            if self.args.need_eval and self.args.eval_strategy == "epoch":
                self.logger.print(
                    f"\n***** Evaluating at epoch {epoch + 1}/{self.args.epochs} *****",
                )
                self.logger.log(self.eval(), step=global_step)

            self.model.tput_timer.update_epoch_count()

    def set_train(self, mode: bool = True) -> None:
        """Set training mode for model."""
        if mode:
            self.model.train()
            if self.args.gradient_checkpointing:
                self.model.gradient_checkpointing_enable()
        else:
            self.model.eval()
            if self.args.gradient_checkpointing:
                self.model.gradient_checkpointing_disable()