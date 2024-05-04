from .modelargs import MixConfig, LLMModelArgs, MultiLoraBatchData
from .lora_linear import get_range_tensor, Linear
from .mix_lora import moe_layer_factory
from .model import LLMFeedForward

from typing import Dict, List, Optional
import torch


class FeedForward(torch.nn.Module):
    def __init__(self, mlp: LLMFeedForward) -> None:
        super().__init__()
        self.mlp_: LLMFeedForward = mlp
        # mix of experts
        self.moes_: torch.ModuleDict = {}

    def state_dict(self) -> Dict[str, Linear]:
        return self.mlp_.state_dict()

    def forward(self, data: torch.Tensor, input_args: MultiLoraBatchData) -> torch.Tensor:
        if len(self.moes_) == 0:
            return self.mlp_._batch_forward(data, input_args), []
        else:
            return self._mixlora_forward(data, input_args)

    # MixLoRA
    def init_moe_weight(self, args: LLMModelArgs, config: MixConfig, gate: Optional[torch.Tensor] = None):
        self.moes_[config.adapter_name] = moe_layer_factory(args, config)
        if gate is None:
            torch.nn.init.normal_(
                self.moes_[config.adapter_name].gate_.weight, mean=0.0, std=config.router_init_range_)
        else:
            with torch.no_grad():
                self.moes_[config.adapter_name].gate_.weight.copy_(gate)

    def _expert_forward_callback(self, moe_name, act_fn, expert_idx, data):
        lora_name = f"moe.{moe_name}.experts.{expert_idx}"
        return self.mlp_._lora_forward(lora_name, act_fn, data)

    def _mixlora_forward(self, data: torch.Tensor, input_args: MultiLoraBatchData):
        final_hidden_states = torch.zeros_like(data)

        if input_args.output_router_logits_:
            router_logits = [None for _ in range(
                len(input_args.lora_batch_data_config_))]
        else:
            router_logits = []

        for idx, lora_config in enumerate(input_args.lora_batch_data_config_):
            moe_name = lora_config.adapter_name_
            start_idx = lora_config.batch_start_idx_
            end_idx = lora_config.batch_end_idx_

            if moe_name in self.moes_:
                current_hidden_states, current_router_outputs = self.moes_[
                    moe_name].forward(self._expert_forward_callback, data[start_idx:end_idx])

                if input_args.output_router_logits_ and current_router_outputs is not None:
                    router_logits[idx] = current_router_outputs
            else:
                current_hidden_states = self.mlp_._lora_forward(
                    moe_name, self.act_, data[start_idx:end_idx])

            final_hidden_states.index_add_(0, get_range_tensor(data.device, data.shape[0])[
                                           start_idx:end_idx], current_hidden_states)

        return final_hidden_states, router_logits
