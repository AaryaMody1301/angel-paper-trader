"""Real CPU LoRA/checkpoint tests on a tiny randomly initialised Qwen3; no downloads."""

import os
from pathlib import Path
import tempfile
import unittest

try:
    import torch
    from transformers import Qwen3Config, Qwen3ForCausalLM
    from peft import LoraConfig, PeftModel, get_peft_model
except ImportError:
    if os.environ.get("REQUIRE_TRAINING_TESTS") == "1":
        raise
    torch = None

from ai_paper.learning import fit_lora


@unittest.skipIf(torch is None, "optional training dependencies absent")
class TrainingTests(unittest.TestCase):
    def check_training(self, device):
        torch.manual_seed(42)
        torch.set_num_threads(2)
        config = Qwen3Config(
            vocab_size=64,
            hidden_size=32,
            intermediate_size=64,
            num_hidden_layers=1,
            num_attention_heads=2,
            num_key_value_heads=1,
            head_dim=16,
        )
        model = get_peft_model(
            Qwen3ForCausalLM(config),
            LoraConfig(
                r=2, lora_alpha=4, lora_dropout=0, target_modules=["q_proj", "v_proj"], task_type="CAUSAL_LM"
            ),
        )
        tokens = torch.tensor([[1, 2, 3, 4, 5, 6, 7, 8]])
        labels = tokens.clone()
        labels[:, :4] = -100
        batches = [{"input_ids": tokens, "attention_mask": torch.ones_like(tokens), "labels": labels}] * 2
        stats = fit_lora(model, batches, device, steps=2, accumulation=1)
        self.assertTrue(stats["frozen_weights_unchanged"])
        self.assertTrue(stats["adapter_changed"])
        self.assertEqual(stats["steps"], 2)
        self.assertGreater(stats["peak_rss_bytes"], 0)
        model.to("cpu").eval()
        with torch.no_grad():
            expected = model(input_ids=tokens).logits
        with tempfile.TemporaryDirectory() as directory:
            model.save_pretrained(directory, safe_serialization=True)
            self.assertTrue((Path(directory) / "adapter_model.safetensors").exists())
            torch.manual_seed(42)
            restored = PeftModel.from_pretrained(Qwen3ForCausalLM(config), directory).eval()
            with torch.no_grad():
                actual = restored(input_ids=tokens).logits
            torch.testing.assert_close(actual, expected)

    def test_cpu_lora_frozen_base_changed_adapter_and_checkpoint_reload(self):
        self.check_training("cpu")

    def test_actual_cuda_when_present(self):
        if not torch.cuda.is_available():
            self.skipTest("no CUDA hardware/build; CPU training verified separately")
        self.check_training("cuda")


if __name__ == "__main__":
    unittest.main()
