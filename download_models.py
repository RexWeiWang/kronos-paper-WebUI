"""Download immutable model snapshots; run once with network access."""
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
os.environ.setdefault("HF_HOME", str(ROOT / ".cache" / "huggingface"))
from huggingface_hub import HfApi, snapshot_download

manifest_path = ROOT / "models" / "manifest.json"
manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
for name in ("Kronos-base", "Kronos-Tokenizer-base"):
    repo = f"NeoQuasar/{name}"
    revision = manifest.get(name, {}).get("revision") or HfApi().model_info(repo).sha
    snapshot_download(repo, revision=revision, local_dir=ROOT / "models" / name,
                      allow_patterns=["config.json", "model.safetensors", "README.md"])
    manifest[name] = {"repo": repo, "revision": revision}
    print(name, revision, flush=True)
manifest_path.write_text(json.dumps(manifest, indent=2))
