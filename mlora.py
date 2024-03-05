# m-LoRA: Efficient Multi-LoRA Fine Tuning with Shared-Based Model
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
#
# Copyright (C) 2023 All Rights Reserved.
#
# Github:  https://github.com/TUDB-Labs/multi-lora-fine-tune

import json
import torch
import mlora
import argparse
import logging

from typing import Dict, List

# Command Line Arguments
parser = argparse.ArgumentParser(description='m-LoRA main program')
parser.add_argument('--base_model', type=str, required=True,
                    help='Path to or name of base model')
parser.add_argument('--tokenizer', type=str,
                    help='Path to or name of tokenizer')
parser.add_argument('--model_type', type=str, default="llama",
                    help='The model type, support: llama, chatglm')
parser.add_argument('--device', type=str, default='cuda:0',
                    help='Specify which GPU to be used, default is cuda:0')
# load quant
parser.add_argument('--load_8bit', action="store_true",
                    help='Load model in 8bit mode')
parser.add_argument('--load_4bit', action="store_true",
                    help='Load model in 4bit mode')
# inference model
parser.add_argument('--inference', action="store_true",
                    help='The inference mode (just for test)')
# mmlu evaluate model
parser.add_argument('--evaluate', type=str,
                    help='Enable the evaluate mode.')
parser.add_argument('--evaluate_data', type=str,
                    help='The evaluate dataset name or path.')
# whether to enable the lora
parser.add_argument('--load_lora', action="store_true",
                    help="Load lora from file instead of init randomly")
parser.add_argument('--disable_lora', action="store_true",
                    help="Disable the lora modules")
# configuration
parser.add_argument('--config', type=str,
                    help='Path to finetune configuration')
parser.add_argument('--seed', type=int, default=42,
                    help='Random seed in integer, default is 42')
# configuration about log
parser.add_argument('--log_level', type=str, default="INFO",
                    help="Set the log level.")
parser.add_argument('--log_file', type=str,
                    help="Save log to specific file.")
# the argument about pipeline
parser.add_argument('--pipeline', action="store_true",
                    help="Train the LoRA model use the pipeline parallelism")
parser.add_argument('--rank', type=int, default=-1,
                    help="The device's rank number")
parser.add_argument('--balance', type=int, nargs="+",
                    help="The model's balance")


args = parser.parse_args()


# to get test result and want early stop it
def train(config: Dict[str, any], llm_model: mlora.LLMModel, dispatcher: mlora.Dispatcher):
    trainer = mlora.Trainer(llm_model, dispatcher, config["lora"])
    trainer.train()


def inference(config: Dict[str, any],
              llm_model: mlora.LLMModel,
              tokenizer: mlora.Tokenizer):
    lora_adapter_num = len(config["lora"])
    batch_data_config: List[mlora.LoraBatchDataConfig] = []

    for idx, lora_config in enumerate(config["lora"]):
        adapter_name = lora_config["name"]
        batch_data_config.append(mlora.LoraBatchDataConfig(
            adapter_name, idx, idx + 1))

    inference_max_len = 128

    while True:
        input_raw = input("INPUT WITHOUT PROMPT: ")
        if input_raw == "QUIT":
            return

        tokens = tokenizer.encode(input_raw, True, False)
        token_len = len(tokens)
        while len(tokens) < inference_max_len:
            tokens.append(tokenizer.pad_id_)

        input_data = mlora.MultiLoraBatchData(
            prompts_=[input_raw] * lora_adapter_num,
            lora_batch_data_config_=batch_data_config,
            batch_tokens_=[tokens] * lora_adapter_num,
            tokens_len_without_pad_=[token_len] * lora_adapter_num,
            batch_seq_len_=inference_max_len,
            expand_side_=["right"] * lora_adapter_num,
            inference_model_=True)

        eos_flag: List[bool] = [False] * lora_adapter_num
        for pos in range(token_len, inference_max_len):
            with torch.no_grad():
                # batch_size, seq_len, voc_logs
                outputs = llm_model.forward(input_data)
                next_token = outputs[:, pos - 1, :]
                next_token = torch.argmax(next_token, dim=-1)
                for idx in range(len(input_data.batch_tokens_)):
                    input_data.batch_tokens_[idx][pos] = next_token[idx].item()
                    # end of the sentence
                    if next_token[idx].item() == tokenizer.eos_id_:
                        eos_flag[idx] = True
                    input_data.tokens_len_without_pad_[
                        idx] = input_data.tokens_len_without_pad_[idx] + 1
            # check if the all sentence end
            have_all_done = all(flag for flag in eos_flag)
            if have_all_done:
                break

        for idx, output in enumerate(input_data.batch_tokens_):
            print(f"# LORA{idx} OUTPUT IS:")
            print(tokenizer.decode(output))


# Main Function
if __name__ == "__main__":
    # set the random seed
    mlora.setup_seed(args.seed)
    mlora.setup_logging(args.log_level, args.log_file)
    mlora.setup_cuda_check()

    # load part of model to device
    partial_model_to_device = None
    if args.pipeline:
        assert args.rank != -1
        assert len(args.balance) >= args.rank
        logging.info(
            f"Pipeline parallelism, rank is {args.rank} and balance is {args.balance}.")

        partial_model_to_device = [
            index + sum(args.balance[:args.rank])for index in range(0, args.balance[args.rank])]

    tokenizer, model = mlora.load_base_model(args.base_model,
                                             args.model_type,
                                             args.device,
                                             args.load_4bit,
                                             args.load_8bit,
                                             partial_model_to_device)

    if not args.disable_lora:
        assert args.config is not None, "error: Argument --config are required."

        with open(args.config, 'r', encoding='utf8') as fp:
            config = json.load(fp)
        mlora.init_lora_model(config, model, args.load_lora)

    if args.pipeline:
        raise NotImplementedError

    if args.inference:
        inference(config, model, tokenizer)
    elif args.evaluate:
        evaluator: mlora.Evaluator = mlora.EvaluatorFactory().create(
            model, tokenizer, args.evaluate, args.evaluate_data)
        evaluator.evaluate()
    else:
        dispatcher = mlora.Dispatcher(config, tokenizer)
        train(config, model, dispatcher)
