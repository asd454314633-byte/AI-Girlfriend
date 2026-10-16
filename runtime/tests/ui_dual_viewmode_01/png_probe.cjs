/* ============================================================================
 * png_probe.cjs — 零依赖 PNG 解码 + 像素采样（只用 node:zlib / node:crypto）
 *
 * 为什么需要它：上一版"截图通道自检"靠比较两张 PNG 的 sha256 是否不同来判定
 * "拍的是不是本页面"。这个判据在本项目里【根本不成立】——页面里有一个正在
 * 播放的真实数字人视频，任意两次捕获的帧本来就不同，错 surface 也一样会变。
 * 于是"哈希变了"既不能证明对，也不能证明错，属于典型的假闸门。
 *
 * 现在改成正向证明：往本 document 注入纯洋红满屏色块，然后【数像素】——
 * 只有真的拍到了这个 document，洋红占比才会接近 100%。
 *
 * 只支持 CDP 实际产出的形状：bit depth 8 / color type 2(真彩) 或 6(带 alpha) /
 * 非隔行。其它情况一律抛错，不猜——猜出来的像素会把验收变成自欺。
 * ==========================================================================*/
'use strict';

const zlib = require('node:zlib');
const crypto = require('node:crypto');

const SIG = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);

function crc32(buf) {
  let c, table = crc32.table;
  if (!table) {
    table = crc32.table = new Int32Array(256);
    for (let n = 0; n < 256; n++) {
      c = n;
      for (let k = 0; k < 8; k++) c = (c & 1) ? (0xedb88320 ^ (c >>> 1)) : (c >>> 1);
      table[n] = c;
    }
  }
  c = 0 ^ -1;
  for (let i = 0; i < buf.length; i++) c = (c >>> 8) ^ table[(c ^ buf[i]) & 0xff];
  return (c ^ -1) >>> 0;
}

/** 解码成 {width,height,channels,data} 的紧凑行缓冲（stride=width*channels）。 */
function decode(buf) {
  if (buf.length < 8 || !buf.subarray(0, 8).equals(SIG)) throw new Error('NOT_PNG');
  let off = 8;
  let ihdr = null;
  const idat = [];
  let plte = null;
  const chunks = [];
  while (off + 8 <= buf.length) {
    const len = buf.readUInt32BE(off);
    // 硬上限：解出 desync 后的垃圾长度字段会当场把内存打爆（历史事故教训）
    if (len > 64 * 1024 * 1024) throw new Error('PNG_CHUNK_TOO_LARGE ' + len);
    const type = buf.toString('latin1', off + 4, off + 8);
    const data = buf.subarray(off + 8, off + 8 + len);
    if (off + 8 + len + 4 > buf.length) throw new Error('PNG_TRUNCATED_AT_' + type);
    const want = crc32(Buffer.concat([Buffer.from(type, 'latin1'), data]));
    if (want !== buf.readUInt32BE(off + 8 + len)) throw new Error('PNG_CRC_FAIL ' + type);
    chunks.push(type);
    if (type === 'IHDR') {
      ihdr = {
        width: data.readUInt32BE(0), height: data.readUInt32BE(4),
        depth: data[8], color: data[9], compress: data[10], filter: data[11], interlace: data[12],
      };
    } else if (type === 'IDAT') idat.push(data);
    else if (type === 'PLTE') plte = data;
    else if (type === 'IEND') break;
    off += 12 + len;
  }
  if (!ihdr) throw new Error('PNG_NO_IHDR');
  if (ihdr.interlace !== 0) throw new Error('INTERLACED_UNSUPPORTED');
  if (ihdr.depth !== 8) throw new Error('DEPTH_UNSUPPORTED_' + ihdr.depth);
  let channels;
  if (ihdr.color === 2) channels = 3;
  else if (ihdr.color === 6) channels = 4;
  else if (ihdr.color === 0) channels = 1;
  else if (ihdr.color === 4) channels = 2;
  else if (ihdr.color === 3 && plte) channels = 1;
  else throw new Error('COLORTYPE_UNSUPPORTED_' + ihdr.color);

  const stride = ihdr.width * channels;
  // 分配前先校验尺寸合法性：length 字段在 desync 后是垃圾值
  if (ihdr.width <= 0 || ihdr.height <= 0 ||
      ihdr.width > 16384 || ihdr.height > 16384 ||
      stride * ihdr.height > 256 * 1024 * 1024) throw new Error('PNG_DIM_SUSPECT');

  let raw;
  try { raw = zlib.inflateSync(Buffer.concat(idat)); }
  catch (e) { throw new Error('PNG_INFLATE_FAIL ' + e.message); }
  if (raw.length < stride * ihdr.height) throw new Error('PNG_DATA_SHORT');

  const out = Buffer.allocUnsafe(stride * ihdr.height);
  let p = 0;
  for (let y = 0; y < ihdr.height; y++) {
    const f = raw[p++];
    const rowStart = y * stride;
    const prevStart = (y - 1) * stride;
    for (let x = 0; x < stride; x++) {
      const bpp = channels;                       // depth=8 ⇒ 每像素恰 channels 字节
      const A = x >= bpp ? out[rowStart + x - bpp] : 0;
      const B = y > 0 ? out[prevStart + x] : 0;
      const C = (x >= bpp && y > 0) ? out[prevStart + x - bpp] : 0;
      const t = raw[p++];
      let v;
      if (f === 0) v = t;
      else if (f === 1) v = t + A;
      else if (f === 2) v = t + B;
      else if (f === 3) v = t + ((A + B) >> 1);
      else if (f === 4) {
        const pp = A + B - C, pa = Math.abs(pp - A), pb = Math.abs(pp - B), pc = Math.abs(pp - C);
        v = t + (pa <= pb && pa <= pc ? A : (pb <= pc ? B : C));
      } else throw new Error('PNG_BAD_FILTER ' + f + ' at row ' + y);
      out[rowStart + x] = v & 0xff;
    }
  }

  if (ihdr.color === 3) {
    const rgb = Buffer.allocUnsafe(ihdr.width * 3);
    for (let x = 0; x < ihdr.width; x++) {
      const i = out[x] * 3;
      rgb[x * 3] = plte[i]; rgb[x * 3 + 1] = plte[i + 1]; rgb[x * 3 + 2] = plte[i + 2];
    }
    return { width: ihdr.width, height: ihdr.height, channels: 3, data: rgb,
             stride: ihdr.width * 3, ihdr, chunks, paletteExpanded: true };
  }
  return { width: ihdr.width, height: ihdr.height, channels, data: out, stride, ihdr, chunks };
}

function pixel(img, x, y) {
  if (x < 0 || y < 0 || x >= img.width || y >= img.height) throw new Error('PX_OUT_OF_RANGE');
  const i = y * img.stride + x * img.channels;
  return [img.data[i], img.data[i + 1], img.data[i + 2]];
}

/**
 * 按步长栅格采样，返回满足 pred(r,g,b) 的像素占比与命中点坐标样本。
 * step=8 时 1920x1080 约采 200x135≈2.7 万点，足够把"0% vs 99%"区分开。
 */
function fraction(img, pred, step = 8) {
  let hit = 0, total = 0; const samples = [];
  for (let y = 0; y < img.height; y += step) {
    for (let x = 0; x < img.width; x += step) {
      const [r, g, b] = pixel(img, x, y);
      total++;
      if (pred(r, g, b)) { hit++; if (samples.length < 5) samples.push([x, y]); }
    }
  }
  return { hit, total, ratio: total ? +(hit / total).toFixed(5) : 0, samples };
}

/** 矩形区域内的占比（坐标为 CSS 像素，捕获 deviceScaleFactor=1 故 1:1）。 */
function fractionInRect(img, rect, pred, step = 4) {
  const x0 = Math.max(0, Math.round(rect.x)), y0 = Math.max(0, Math.round(rect.y));
  const x1 = Math.min(img.width, Math.round(rect.x + rect.w));
  const y1 = Math.min(img.height, Math.round(rect.y + rect.h));
  let hit = 0, total = 0;
  for (let y = y0; y < y1; y += step) for (let x = x0; x < x1; x += step) {
    const [r, g, b] = pixel(img, x, y);
    total++; if (pred(r, g, b)) hit++;
  }
  return { hit, total, ratio: total ? +(hit / total).toFixed(5) : 0, rect: { x0, y0, x1, y1 } };
}

const isMagenta = (r, g, b) => r >= 235 && g <= 30 && b >= 235;
/** "非黑"= 至少一个通道 > 24，用来证明人物画面区域真的有内容而不是全黑。 */
const isNotBlack = (r, g, b) => r > 24 || g > 24 || b > 24;
/** "够亮"= 感知亮度 > 90，用来证明控制面板（文字/边框）在图上看得见。 */
const isBright = (r, g, b) => (r * 299 + g * 587 + b * 114) / 1000 > 90;

function luminanceSpread(img, step = 16) {
  let min = 255, max = 0, sum = 0, n = 0;
  for (let y = 0; y < img.height; y += step) for (let x = 0; x < img.width; x += step) {
    const [r, g, b] = pixel(img, x, y);
    const l = (r * 299 + g * 587 + b * 114) / 1000;
    if (l < min) min = l; if (l > max) max = l; sum += l; n++;
  }
  return { min: +min.toFixed(1), max: +max.toFixed(1), mean: +(sum / n).toFixed(1), n };
}

const sha = (buf) => crypto.createHash('sha256').update(buf).digest('hex').slice(0, 16);

module.exports = { decode, pixel, fraction, fractionInRect, isMagenta, isNotBlack, isBright,
                   luminanceSpread, sha };
