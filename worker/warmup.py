"""Prepare both allowlisted neural checkpoints before offline production use."""
from beat_this.inference import load_checkpoint

if __name__ == "__main__":
    for name in ("small0", "final0"):
        load_checkpoint(name, "cpu")
        print(f"Checkpoint {name} ready", flush=True)
