#!/bin/sh
set -eu

repo_root=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
model_dir="$repo_root/local-data/models/bge-small-en-v1.5"
revision=5c38ec7c405ec4b44b94cc5a9bb96e735b38267a
files='README.md config.json config_sentence_transformers.json modules.json sentence_bert_config.json special_tokens_map.json tokenizer.json tokenizer_config.json vocab.txt 1_Pooling/config.json model.safetensors'

for file in $files; do
  target="$model_dir/$file"
  mkdir -p "$(dirname -- "$target")"
  curl --fail --silent --show-error --location --retry 3 \
    "https://huggingface.co/BAAI/bge-small-en-v1.5/resolve/$revision/$file" \
    --output "$target.partial"
  mv "$target.partial" "$target"
done

test -s "$model_dir/model.safetensors"
python3 "$repo_root/scripts/verify_embedding_model.py" \
  --model-dir "$model_dir" --revision "$revision"
printf 'Cached BAAI/bge-small-en-v1.5 at %s\n' "$model_dir"
