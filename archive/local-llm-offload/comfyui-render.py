#!/usr/bin/env python3
"""Queue-allowlisted runner that renders a txt2img/img2img job on the ComfyUI
server (Unraid RTX 3080) instead of the local MPS pipeline. Same task-JSON shape
as txt2img_exec.py, same status.json/output layout, so /api/jobs + /api/result in
the Pet Portrait Studio backend work unchanged.

The ollama queue invokes it with --model --host --num-ctx --cwd --task-file.
Talks to ComfyUI purely over HTTP: POST /prompt, poll /history, GET /view.

task JSON:
 {job_id, prompt, negative, model(path or basename), steps, guidance, width,
  height, count, seed, out_dir, [init_image, strength]}
"""
import argparse, json, os, sys, time, uuid, urllib.request, urllib.parse
from pathlib import Path

COMFY = os.environ.get("COMFYUI_URL", "http://10.0.12.51:8188")


def _post(path, obj):
    r = urllib.request.Request(COMFY + path, data=json.dumps(obj).encode(),
                               headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(r, timeout=30) as resp:
        return json.loads(resp.read().decode())


def _get(path):
    with urllib.request.urlopen(COMFY + path, timeout=30) as resp:
        return json.loads(resp.read().decode())


def _upload_image(fpath):
    """Upload an init image to ComfyUI, return the server-side filename."""
    import mimetypes
    boundary = "----comfy" + uuid.uuid4().hex
    fname = Path(fpath).name
    data = open(fpath, "rb").read()
    body = (
        ("--%s\r\nContent-Disposition: form-data; name=\"image\"; filename=\"%s\"\r\n"
         "Content-Type: application/octet-stream\r\n\r\n" % (boundary, fname)).encode()
        + data
        + ("\r\n--%s\r\nContent-Disposition: form-data; name=\"overwrite\"\r\n\r\ntrue\r\n--%s--\r\n"
           % (boundary, boundary)).encode()
    )
    r = urllib.request.Request(COMFY + "/upload/image", data=body,
                               headers={"Content-Type": "multipart/form-data; boundary=" + boundary},
                               method="POST")
    with urllib.request.urlopen(r, timeout=60) as resp:
        return json.loads(resp.read().decode())["name"]


def _sdxl_txt2img(job, ckpt, seed):
    w, h = int(job.get("width", 1024)), int(job.get("height", 1024))
    return {
        "4": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": ckpt}},
        "6": {"class_type": "CLIPTextEncode", "inputs": {"text": job["prompt"], "clip": ["4", 1]}},
        "7": {"class_type": "CLIPTextEncode", "inputs": {"text": job.get("negative", ""), "clip": ["4", 1]}},
        "5": {"class_type": "EmptyLatentImage", "inputs": {"width": w, "height": h, "batch_size": 1}},
        "3": {"class_type": "KSampler", "inputs": {
            "seed": seed, "steps": int(job.get("steps", 30)), "cfg": float(job.get("guidance", 6.5)),
            "sampler_name": "dpmpp_2m", "scheduler": "karras", "denoise": 1.0,
            "model": ["4", 0], "positive": ["6", 0], "negative": ["7", 0], "latent_image": ["5", 0]}},
        "8": {"class_type": "VAEDecode", "inputs": {"samples": ["3", 0], "vae": ["4", 2]}},
        "9": {"class_type": "SaveImage", "inputs": {"filename_prefix": job["job_id"], "images": ["8", 0]}},
    }


def _sdxl_img2img(job, ckpt, seed, server_img):
    return {
        "4": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": ckpt}},
        "6": {"class_type": "CLIPTextEncode", "inputs": {"text": job["prompt"], "clip": ["4", 1]}},
        "7": {"class_type": "CLIPTextEncode", "inputs": {"text": job.get("negative", ""), "clip": ["4", 1]}},
        "10": {"class_type": "LoadImage", "inputs": {"image": server_img}},
        "11": {"class_type": "VAEEncode", "inputs": {"pixels": ["10", 0], "vae": ["4", 2]}},
        "3": {"class_type": "KSampler", "inputs": {
            "seed": seed, "steps": int(job.get("steps", 30)), "cfg": float(job.get("guidance", 6.5)),
            "sampler_name": "dpmpp_2m", "scheduler": "karras", "denoise": float(job.get("strength", 0.55)),
            "model": ["4", 0], "positive": ["6", 0], "negative": ["7", 0], "latent_image": ["11", 0]}},
        "8": {"class_type": "VAEDecode", "inputs": {"samples": ["3", 0], "vae": ["4", 2]}},
        "9": {"class_type": "SaveImage", "inputs": {"filename_prefix": job["job_id"], "images": ["8", 0]}},
    }


def _write_status(sdir, **kw):
    (sdir / "status.json").write_text(json.dumps(kw))


def main():
    ap = argparse.ArgumentParser()
    for f in ("--model", "--host", "--num-ctx", "--cwd", "--task-file"):
        ap.add_argument(f)
    a = ap.parse_args()
    job = json.loads(Path(a.task_file).read_text())
    out = Path(job["out_dir"]) / job["job_id"]
    out.mkdir(parents=True, exist_ok=True)
    count = int(job.get("count", 1))
    base_seed = int(job.get("seed", 1000))
    ckpt = Path(job["model"]).name if "/" in str(job.get("model", "")) else job.get("model")
    results = [{"idx": i, "done": False, "url": None} for i in range(count)]
    _write_status(out, status="running", results=results, error=None)

    try:
        server_img = _upload_image(job["init_image"]) if job.get("init_image") else None
        client_id = uuid.uuid4().hex
        for i in range(count):
            seed = base_seed + i * 13
            wf = (_sdxl_img2img(job, ckpt, seed, server_img) if server_img
                  else _sdxl_txt2img(job, ckpt, seed))
            t0 = time.time()
            pid = _post("/prompt", {"prompt": wf, "client_id": client_id})["prompt_id"]
            # poll history until this prompt completes
            imgs = None
            while imgs is None:
                time.sleep(1.5)
                hist = _get("/history/" + pid)
                if pid in hist:
                    node9 = hist[pid].get("outputs", {}).get("9", {})
                    imgs = node9.get("images", [])
            if not imgs:
                raise RuntimeError("ComfyUI returned no image for prompt " + pid)
            im = imgs[0]
            q = urllib.parse.urlencode({"filename": im["filename"], "subfolder": im.get("subfolder", ""),
                                        "type": im.get("type", "output")})
            with urllib.request.urlopen(COMFY + "/view?" + q, timeout=60) as resp:
                (out / f"{i}.png").write_bytes(resp.read())
            results[i] = {"idx": i, "done": True, "url": f"/api/result/{job['job_id']}/{i}.png",
                          "secs": round(time.time() - t0)}
            _write_status(out, status="running", results=results, error=None)
            print(f"[comfyui] {i}.png ({results[i]['secs']}s)", flush=True)
        _write_status(out, status="done", results=results, error=None)
    except Exception as e:
        _write_status(out, status="error", results=results, error=str(e))
        import traceback; traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
