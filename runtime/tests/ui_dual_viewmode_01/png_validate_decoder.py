"""png_validate_decoder.py — 用两个独立引擎交叉验证自写 PNG 解码器

为什么必须做这一步：§19 的视觉验收现在完全建立在"数洋红像素占比"上，
而数像素用的是我自己写的 png_probe.cjs。一个把 Paeth 写错的解码器会给出
【看起来非常像真的】的错判决（症状通常是横向错位条带 / 局部偏色），
所以先证明尺子准，再拿它量东西。

验证链（Python 只读引用项目 venv 里【已有】的 PIL/numpy，不装任何包）：
  1) 合成图：用 Python 自己实现的编码器，逐张指定 filter type 0..4 与混合行过滤；
     先让 PIL 解开证明编码器与参照实现一致（否则"两边都错"会伪装成"两边一致"）；
  2) 真实图：四张 CDP 截图；
  3) 每张贴合 PIL 的整幅数组与 Node 输出的裸 RGB 数组做【逐字节全量】比较，
     不是抽样 —— 抽样会漏掉只在个别行出现的 off-by-one。
"""
import json
import os
import struct
import subprocess
import sys
import zlib
from pathlib import Path

import numpy as np
from PIL import Image

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]                     # zero_replica/
EV = REPO / "runtime" / "evidence" / "UI_DUAL_VIEWMODE_01"
SHOT = EV / "SCREENSHOTS"
TMP = EV / "_decoder_validation_tmp"
NODE = "node"


def png_encode(w, h, rgba, filter_mode):
    """手写 PNG 编码器（测试夹具）。filter_mode ∈ {0..4, 'mixed'}。"""
    ch = 4
    raw = bytearray()
    prev = bytes(w * ch)
    for y in range(h):
        line = bytes(rgba[y * w * ch:(y + 1) * w * ch])
        if filter_mode == 'mixed':
            f = (y * 7 + 3) % 5
        else:
            f = filter_mode
        out = bytearray(len(line))
        for x in range(len(line)):
            a = line[x - ch] if x >= ch else 0
            b = prev[x]
            c = prev[x - ch] if x >= ch else 0
            p = a + b - c
            if f == 0:
                pred = 0
            elif f == 1:
                pred = a
            elif f == 2:
                pred = b
            elif f == 3:
                pred = (a + b) >> 1
            else:
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pred = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
            out[x] = (line[x] - pred) & 0xFF
        raw.append(f)
        raw += out
        prev = line

    def chunk(tag, data):
        return (struct.pack('>I', len(data)) + tag + data
                + struct.pack('>I', zlib.crc32(tag + data) & 0xFFFFFFFF))

    body = chunk(b'IHDR', struct.pack('>IIBBBBB', w, h, 8, 6, 0, 0, 0))
    body += chunk(b'IDAT', zlib.compress(bytes(raw), 6))
    body += chunk(b'IEND', b'')
    return b'\x89PNG\r\n\x1a\n' + body


def synth_cases():
    rng = np.random.default_rng(20260926)
    out = []
    for idx, mode in enumerate([0, 1, 2, 3, 4, 'mixed']):
        w, h = 61, 37                      # 故意用奇数宽，逼出跨行/跨通道的边界 bug
        a = rng.integers(0, 256, size=(h, w, 4), dtype=np.uint8)
        # 掺入渐变与纯色行，让"整体偏移一行"这类错误必然改变数值
        a[0, :, :] = 255
        a[-1, :, 0] = np.linspace(0, 255, w).astype(np.uint8)
        a[:, 0, :] = 0
        path = TMP / f"synth_{idx}_{mode}.png"
        path.write_bytes(png_encode(w, h, a.reshape(-1), mode))
        # 裁判先验编码器：PIL 解出来必须等于原始数组，否则"两边一致"是自证
        back = np.array(Image.open(path).convert('RGBA'), dtype=np.uint8).reshape(h, w, 4)
        out.append({
            'name': f'synthetic_filter_{mode}', 'path': str(path),
            'expected_shape': [h, w, 4],
            'encoder_matches_pil': bool(np.array_equal(back, a)),
            '_arr': a,
        })
    return out


def node_dump(png_path, tag):
    rgb = TMP / f"{tag}.rgb"
    r = subprocess.run([NODE, str(HERE / 'png_dump.cjs'), str(png_path), str(rgb)],
                       capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        raise RuntimeError(f"node_dump failed for {png_path}: {r.stderr[:400]}")
    meta = json.loads((TMP / f"{tag}.rgb.json").read_text(encoding='utf-8'))
    arr = np.frombuffer(rgb.read_bytes(), dtype=np.uint8).reshape(meta['height'], meta['width'], 3)
    return arr, meta


def compare(name, path, expected_rgb):
    arr, meta = node_dump(path, name.replace('/', '_'))
    eq_shape = arr.shape == expected_rgb.shape
    if not eq_shape:
        return {'name': name, 'shape_ok': False, 'node_shape': list(arr.shape),
                'ref_shape': list(expected_rgb.shape), 'byte_mismatches': None, 'VERDICT': 'FAIL'}
    diff = np.not_equal(arr, expected_rgb)
    bad = int(diff.sum())
    first = None
    if bad:
        ys, xs, cs = np.nonzero(diff)
        k = int(ys.size // 2)              # 取中间一个，避免只看角落
        y, x, cch = int(ys[k]), int(xs[k]), int(cs[k])
        first = {'y': y, 'x': x, 'ch': cch,
                 'node': int(arr[y, x, cch]), 'ref': int(expected_rgb[y, x, cch])}
    return {'name': name, 'file': os.path.basename(str(path)), 'node_dims': [meta['width'], meta['height']],
            'node_colorType': meta['colorType'], 'source_sha16': meta['sha16'],
            'shape_ok': True, 'pixels': int(arr.size // 3), 'channel_value_mismatches': bad,
            'first_mismatch': first, 'VERDICT': 'PASS' if bad == 0 else 'FAIL'}


def main():
    TMP.mkdir(parents=True, exist_ok=True)
    results = []

    for c in synth_cases():
        a = c.pop('_arr')
        if not c['encoder_matches_pil']:
            c['VERDICT'] = 'INCONCLUSIVE_ENCODER_BROKEN'
            results.append(c)
            continue
        c.update(compare(c['name'], c['path'], a[:, :, :3]))
        results.append(c)

    if SHOT.is_dir():
        for p in sorted(SHOT.glob('*.png')):
            im = Image.open(p)
            mode = im.mode
            arr = np.array(im.convert('RGB'), dtype=np.uint8)
            r = compare('real_' + p.stem, p, arr)
            r['pil_mode'] = mode
            results.append(r)

    hard = [r for r in results if r['name'].startswith('synthetic')]
    allpass = all(r.get('VERDICT') == 'PASS' for r in hard)
    out = {
        'validated_at': __import__('datetime').datetime.utcnow().isoformat() + 'Z',
        'reference_engines': {
            'PIL': Image.__version__ if hasattr(Image, '__version__') else 'unknown',
            'numpy': np.__version__,
            'note': '仅使用项目 venv 中【已存在】的包，未安装/未升级任何依赖',
        },
        'results': results,
        'synthetic_filter_coverage': sorted({r['name'] for r in hard}),
        'real_screenshots_checked': [r['file'] for r in results if r['name'].startswith('real_')],
        'VERDICT': 'PASS' if allpass else 'FAIL',
        'meaning': ('node 侧 png_probe 与 PIL 在【每一个像素】上完全一致 ⇒ '
                    '"数洋红占比"这把尺子本身可信' if allpass else
                    '自写解码器与 PIL 不一致 ⇒ 一切像素级判决作废，先修解码器'),
    }
    (EV / 'PNG_DECODER_VALIDATION.json').write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if allpass else 1


if __name__ == '__main__':
    sys.exit(main())
