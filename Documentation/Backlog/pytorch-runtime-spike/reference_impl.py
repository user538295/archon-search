"""SPIKE, not production code. Reference implementation of the planned PyTorch embedder/reranker.

Reproduces every golden output in tests/fixtures/runtime_parity/ (fastembed 0.9.0 behaviour) using ONLY:
raw `tokenizers` (no AutoTokenizer), stock `transformers` model classes (no trust_remote_code, no einops),
fp32, and the per-model parameters in model_specs.json. The Jina reranker is loaded by converting its
fused-QKV checkpoint in memory into the stock XLMRobertaForSequenceClassification layout.

Usage: HF_HOME=<dir that already holds every torch_repo snapshot at the pinned revision> python reference_impl.py cpu|mps
Measured 2026-10-24 on M4 Pro, torch 2.14.1, transformers 5.20.2 (CPU and MPS): embedders cos >= 0.999945
(0.999992 or better except bge-base, whose fastembed ONNX is quantized), norm ratio 1.0, reranker max |diff| 1.2e-05.
"""
import glob
import json
import os
import sys

import numpy as np
import torch

os.environ["HF_HUB_OFFLINE"]="1"
from safetensors import safe_open
from tokenizers import Tokenizer
from transformers import (
    AutoModel,
    AutoModelForSequenceClassification,
    XLMRobertaConfig,
    XLMRobertaForSequenceClassification,
)


def _load_json(path):
    with open(path) as fh:
        return json.load(fh)

FX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "tests", "fixtures", "runtime_parity") + os.sep
corpus = _load_json(FX+"corpus.json"); specs = _load_json(FX+"model_specs.json"); gold = np.load(FX+"embedder_golden.npz"); rgold = _load_json(FX+"reranker_golden.json")
dev = sys.argv[1]
def snap(repo, rev): return glob.glob(os.path.join(os.environ.get("HF_HOME", os.path.expanduser("~/.cache/huggingface")), "hub", f"models--{repo.replace('/','--')}", "snapshots", rev))[0]
def load_tok(p, max_len):
    tk = Tokenizer.from_file(p+"/tokenizer.json"); tk.no_padding(); tk.enable_truncation(max_len); return tk   # truncation 'longest_first' is the default strategy
def batch(encs, pad_id, with_types):
    L = max(len(e.ids) for e in encs); n=len(encs)
    ids = torch.full((n,L), pad_id, dtype=torch.long); am = torch.zeros((n,L), dtype=torch.long); tt = torch.zeros((n,L), dtype=torch.long)
    for i,e in enumerate(encs):
        k=len(e.ids); ids[i,:k]=torch.tensor(e.ids); am[i,:k]=1; tt[i,:k]=torch.tensor(e.type_ids)
    d = {"input_ids":ids.to(dev), "attention_mask":am.to(dev)}
    if with_types: d["token_type_ids"]=tt.to(dev)
    return d
print(f"=== embedders on {dev}")
for name, s in specs["embedders"].items():
    p = snap(s["torch_repo"], s["revision"]); tk = load_tok(p, s["max_length"]); m = AutoModel.from_pretrained(p, dtype=torch.float32).to(dev).eval()
    pad = m.config.pad_token_id or 0; has_tt = m.config.type_vocab_size > 1
    out=[]
    with torch.inference_mode():
        for t in corpus["texts"]:   # one by one: worst case for batching assumptions
            b = batch([tk.encode(t)], pad, has_tt); h = m(**b).last_hidden_state; am = b["attention_mask"][...,None]
            v = h[:,0] if s["pooling"]=="cls" else (h*am).sum(1)/am.sum(1)
            if s["normalize"]: v = torch.nn.functional.normalize(v, dim=-1)
            out.append(v[0].float().cpu().numpy())
    a = np.array(out); g = gold[name.replace('/','__')]
    cos = (a*g).sum(1)/(np.linalg.norm(a,axis=1)*np.linalg.norm(g,axis=1)); nr = np.linalg.norm(a,axis=1)/np.linalg.norm(g,axis=1)
    print(f"{name}: cos min {cos.min():.6f} | norm ratio {nr.min():.5f}-{nr.max():.5f} | dim {a.shape[1]}", flush=True)
    del m
print(f"=== rerankers on {dev}")
def jina_native(p):
    cfg = _load_json(p+"/config.json"); H=cfg["hidden_size"]; L=cfg["num_hidden_layers"]; out={}
    with safe_open(p+"/model.safetensors","pt") as f:
        g=f.get_tensor
        for k in ["classifier.dense.weight","classifier.dense.bias","classifier.out_proj.weight","classifier.out_proj.bias","roberta.embeddings.word_embeddings.weight","roberta.embeddings.token_type_embeddings.weight","roberta.embeddings.position_embeddings.weight"]: out[k]=g(k).float()
        out["roberta.embeddings.LayerNorm.weight"]=g("roberta.emb_ln.weight").float(); out["roberta.embeddings.LayerNorm.bias"]=g("roberta.emb_ln.bias").float()
        for i in range(L):
            s=f"roberta.encoder.layers.{i}."; d=f"roberta.encoder.layer.{i}."
            for kind in ("weight","bias"):
                q,k_,v=g(s+f"mixer.Wqkv.{kind}").float().chunk(3,dim=0)
                out[d+f"attention.self.query.{kind}"]=q; out[d+f"attention.self.key.{kind}"]=k_; out[d+f"attention.self.value.{kind}"]=v
                out[d+f"attention.output.dense.{kind}"]=g(s+f"mixer.out_proj.{kind}").float(); out[d+f"attention.output.LayerNorm.{kind}"]=g(s+f"norm1.{kind}").float()
                out[d+f"intermediate.dense.{kind}"]=g(s+f"mlp.fc1.{kind}").float(); out[d+f"output.dense.{kind}"]=g(s+f"mlp.fc2.{kind}").float(); out[d+f"output.LayerNorm.{kind}"]=g(s+f"norm2.{kind}").float()
    c = XLMRobertaConfig(vocab_size=cfg["vocab_size"],hidden_size=H,num_hidden_layers=L,num_attention_heads=cfg["num_attention_heads"],intermediate_size=cfg["intermediate_size"],max_position_embeddings=cfg["max_position_embeddings"],type_vocab_size=cfg["type_vocab_size"],layer_norm_eps=cfg["layer_norm_eps"],pad_token_id=1,bos_token_id=0,eos_token_id=2,num_labels=1)
    m = XLMRobertaForSequenceClassification(c); res = m.load_state_dict(out, strict=False); assert not res.unexpected_keys and all("position_ids" in k for k in res.missing_keys), res
    return m.float()
for name, s in specs["rerankers"].items():
    p = snap(s["torch_repo"], s["torch_revision_sha"]); tk = load_tok(p, s["max_length"])
    m = jina_native(p) if "jina" in name else AutoModelForSequenceClassification.from_pretrained(p, dtype=torch.float32)
    m = m.to(dev).eval(); pad = m.config.pad_token_id or 0; has_tt = m.config.type_vocab_size > 1
    sc=[]
    with torch.inference_mode():
        encs = [tk.encode(q, d) for q,d in corpus["pairs"]]
        for e in encs: sc.append(m(**batch([e], pad, has_tt)).logits[0,0].item())
        bs = m(**batch(encs, pad, has_tt)).logits[:,0].float().cpu().numpy()   # also one big padded batch
    g = np.array(rgold[name]); print(f"{name}: max|diff| single {np.abs(np.array(sc)-g).max():.2e} | one padded batch {np.abs(bs-g).max():.2e}", flush=True)
    del m
