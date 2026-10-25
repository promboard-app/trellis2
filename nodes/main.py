"""promboard.trellis2 — TRELLIS.2(Microsoft) · Pixal3D 이미지 → PBR 텍스처 GLB 노드.

공식 TRELLIS.2 코드는 리눅스에서 CUDA 확장을 컴파일해야 해서, 윈도우에서는 trellis.cpp(pwilkin, MIT)의 미리 빌드된
실행 파일을 쓴다. 실행 파일 zip 과 GGUF 가중치는 플러그인에 넣지 않고 실행 준비가 원래 출처(GitHub 릴리스 · Hugging Face)에서
sha256 을 확인하며 받는다(manifest requires.models). 이 파일은
  1) 받은 zip 을 플러그인 캐시 폴더에 한 번 풀고(폴더 밖으로 나가는 항목은 거절),
  2) 입력 그림을 PNG 로 바꿔 trellis-cli 를 셸 없이 인자 목록으로 부르고,
  3) 표준 출력의 단계 줄([n/7])로 진행률을 알리고, 취소하면 프로세스를 끝낸다.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import time
import zipfile
from collections import deque
from typing import Dict, List, Optional

RELEASE = "v0.8.1"
# 엔진 → 실행 파일 zip 의 모델 이름(= manifest url 모델의 path)
ENGINES: Dict[str, str] = {
    "cuda": f"trellis.cpp-{RELEASE}/trellis-cuda-windows-x64.zip",  # CUDA 13 런타임(드라이버 580 이상)
    "cuda12": f"trellis.cpp-{RELEASE}/trellis-cuda12-windows-x64.zip",
    "vulkan": f"trellis.cpp-{RELEASE}/trellis-vulkan-windows-x64.zip",
}
GGUF_REPO = "ilintar/trellis2-gguf"
PIXAL_REPO = "vegax87/Pixal3D"
# trellis.cpp 가 --models 폴더에서 찾는 파일(TRELLIS.2 한 벌)
BASE_FILES = ["birefnet.gguf", "dinov3.gguf", "ss_dec.gguf", "shape_dec.gguf", "tex_dec.gguf"]
FLOW_FILES = ["ss_flow.gguf", "shape_flow_512.gguf", "shape_flow_1024.gguf", "tex_flow_512.gguf", "tex_flow_1024.gguf"]
PIXAL_FILES = ["pixal3d_naf.gguf", "pixal3d_ss_flow.gguf", "pixal3d_shape_flow_512.gguf", "pixal3d_shape_flow_1024.gguf", "pixal3d_tex_flow_1024.gguf"]
# 경량화 판 → GGUF 폴더(저장소 안 경로 앞부분). Pixal3D 는 f16 하나뿐이라 경량화를 보지 않는다.
QUANT_DIR = {"f16": "", "q8": "q8/", "q4": "q4/"}

STEP_RE = re.compile(r"^\[(\d+)/(\d+)\]")
OOM_RE = re.compile(r"out of memory|ErrorOutOfDeviceMemory|cudaMalloc|failed to allocate|allocation failed|bad_alloc", re.I)
OOM_HELP = (
    "그래픽카드 메모리가 모자랍니다. '해상도'를 512 로 낮추거나, 판을 TRELLIS.2 로, 경량화를 'GGUF Q4' 로 바꾸거나, "
    "다른 프로그램이 그래픽카드를 쓰고 있으면 닫아 주세요."
)


def _extract(zip_path: str, dest: str, ctx) -> str:
    """zip 을 dest 에 한 번 푼다(다 풀린 뒤 표식 파일을 남긴다). 항목이 dest 밖을 가리키면 거절한다."""
    marker = os.path.join(dest, ".promboard-extracted")
    if os.path.isfile(marker):
        return dest
    tmp = dest + ".partial"
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp, exist_ok=True)
    root = os.path.realpath(tmp)
    with zipfile.ZipFile(zip_path) as zf:
        members = zf.infolist()
        for i, info in enumerate(members):
            target = os.path.realpath(os.path.join(root, info.filename))
            if target != root and not target.startswith(root + os.sep):
                raise ValueError(f"실행 파일 묶음에 폴더 밖을 가리키는 항목이 있습니다: {info.filename}")
            zf.extract(info, root)
            if i % 20 == 0:
                ctx.progress(0.02 + 0.06 * i / max(1, len(members)), "unpack")
                ctx.check_cancel()
    shutil.rmtree(dest, ignore_errors=True)
    os.replace(tmp, dest)
    with open(marker, "w", encoding="utf-8") as fh:
        fh.write(os.path.basename(zip_path))
    return dest


def _find_cli(folder: str) -> str:
    for base, _dirs, files in os.walk(folder):
        for f in files:
            if f.lower() == "trellis-cli.exe":
                return os.path.join(base, f)
    raise ValueError("받은 trellis.cpp 묶음에서 trellis-cli.exe 를 찾지 못했습니다.")


def _models_dir(ctx, model: str, quant: str) -> str:
    """trellis-cli --models 폴더. TRELLIS.2 는 받은 저장소의 경량화 판 폴더 그대로, Pixal3D 는 공용 f16 파일과 Pixal3D 파일을 한 폴더에 모은다."""
    if model != "pixal3d":
        return os.path.dirname(ctx.model_path(f"{GGUF_REPO}/{QUANT_DIR[quant]}ss_flow.gguf"))
    if not ctx.cache_dir:
        raise ValueError("Pixal3D 는 모델 파일을 한 폴더에 모을 캐시 폴더가 필요합니다. 로컬 AI 모델 폴더를 확인해 주세요.")
    merged = os.path.join(ctx.cache_dir, "pixal3d-models")
    os.makedirs(merged, exist_ok=True)
    pairs = [(ctx.model_path(f"{GGUF_REPO}/{f}"), f) for f in BASE_FILES]
    pairs += [(ctx.model_path(f"{PIXAL_REPO}/{f}"), f) for f in PIXAL_FILES]
    for src, name in pairs:
        dst = os.path.join(merged, name)
        if os.path.isfile(dst) and os.path.getsize(dst) == os.path.getsize(src):
            continue
        if os.path.exists(dst):
            os.remove(dst)
        try:
            os.link(src, dst)  # 같은 드라이브면 복사 없이 하드 링크
        except OSError:
            shutil.copyfile(src, dst)
    return merged


def _input_png(path: str, dst: str) -> None:
    """trellis-cli 는 png · jpg 를 읽는다 — 다른 형식(webp 등)도 PNG 로 바꿔 넘긴다. 투명도는 그대로 둔다."""
    from PIL import Image, ImageOps

    with Image.open(path) as im:
        im = ImageOps.exif_transpose(im)
        im = im.convert("RGBA" if ("A" in im.getbands() or "transparency" in im.info) else "RGB")
        im.save(dst, "PNG")


def image_to_3d(ctx, inputs, settings):
    model = str(settings["model"])
    quant = str(settings["quantization"])  # 서버가 '자동'을 이 PC 판으로 풀어 보낸다
    engine = str(settings["engine"])
    res = int(settings["resolution"])
    seed = int(settings["seed"])
    texture = bool(settings["texture"])

    ctx.progress(0.01, "prepare")
    if not ctx.cache_dir:
        raise ValueError("실행 파일을 풀 캐시 폴더가 없습니다. 로컬 AI 모델 폴더를 확인해 주세요.")
    zip_path = ctx.model_path(ENGINES[engine])
    run_dir = _extract(zip_path, os.path.join(ctx.cache_dir, f"trellis-{RELEASE}-{engine}"), ctx)
    cli = _find_cli(run_dir)
    models_dir = _models_dir(ctx, model, quant)

    src = ctx.in_path(inputs["image"])
    in_png = ctx.out_path("input.png")
    _input_png(src, in_png)
    out_glb = ctx.out_path("model.glb")

    # 면 수는 trellis-cli 기본(512 → 15만 · 1024 이상 → 30만, quadric 줄이기)을 따른다 — 면 수를 정하는 옵션이 없다.
    args: List[str] = [cli, in_png, out_glb, "--models", models_dir, "--res", str(res), "--seed", str(seed), "--gpu", "0"]
    if engine in ("cuda", "cuda12"):
        args.append("--require-gpu")
    if model == "pixal3d":
        args += ["--model", "pixal3d"]
    if not texture:
        args.append("--no-texture")
    ctx.log("run " + " ".join(os.path.basename(a) if os.path.isabs(a) else a for a in args))

    work = os.path.join(ctx.cache_dir, "run")
    os.makedirs(work, exist_ok=True)
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    started = time.monotonic()
    ctx.progress(0.1, "load-model")
    proc = subprocess.Popen(args, cwd=work, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL, creationflags=flags)
    tail: deque = deque(maxlen=40)
    state = {"step": 0, "total": 7}

    def reader() -> None:
        assert proc.stdout is not None
        for raw in iter(proc.stdout.readline, b""):
            line = raw.decode("utf-8", "replace").rstrip()
            if not line:
                continue
            tail.append(line)
            m = STEP_RE.match(line)
            if m:
                state["step"], state["total"] = int(m.group(1)), max(1, int(m.group(2)))
                ctx.log(line[:200])

    th = threading.Thread(target=reader, daemon=True)
    th.start()
    last_step = -1
    try:
        while proc.poll() is None:
            if ctx.cancelled:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                ctx.check_cancel()
            if state["step"] != last_step:
                last_step = state["step"]
                ctx.progress(0.1 + 0.88 * max(0, last_step - 1) / state["total"], f"step-{last_step}")
            time.sleep(0.25)
    finally:
        if proc.poll() is None:
            proc.kill()
        th.join(timeout=5)
    ctx.check_cancel()
    text = "\n".join(tail)
    if proc.returncode != 0 or not os.path.isfile(out_glb):
        ctx.log("trellis-cli failed:\n" + text[-3000:], "error")
        if OOM_RE.search(text):
            raise RuntimeError(OOM_HELP)
        if "no voxels produced" in text:
            raise ValueError("그림에서 물체를 찾지 못했습니다. 물체 하나가 가운데에 크게 나온 그림(배경이 단순하거나 투명)을 넣어 주세요.")
        last = next((l for l in reversed(tail) if l.strip()), "")
        raise RuntimeError(f"trellis-cli 가 실패했습니다(종료 코드 {proc.returncode}). {last[:160]}")
    ctx.log(f"done in {time.monotonic() - started:.1f}s, glb {os.path.getsize(out_glb) / 1e6:.1f}MB")
    ctx.progress(1.0, "done")
    return {"model": out_glb}


NODES = {"image-to-3d": image_to_3d}
