// 高频轨迹读取与沿轨道显示插值；不参与仿真，不跨停车空档制造运动。
export class MotionPlayer {
  static async open(url) {
    const response = await fetch(url);
    if (!response.ok) throw new Error('无法加载车辆轨迹索引');
    return new MotionPlayer(await response.json(), new URL(url, location.href));
  }
  constructor(index, url) {
    this.index = index; this.base = new URL('.', url);
    this.cache = new Map(); this.pending = new Map(); this.tracksIndex = -1;
    this.ids = index.vehicle_ids; this.edges = index.edges;
  }
  chunkIndex(time) {
    let low = 0, high = this.index.chunks.length;
    while (low < high) { const mid = (low + high) >> 1;
      if (this.index.chunks[mid].start <= time) low = mid + 1; else high = mid;
    }
    return Math.max(0, low - 1);
  }
  async load(i) {
    if (i >= this.index.chunks.length || this.cache.has(i)) return;
    if (this.pending.has(i)) return this.pending.get(i);
    const promise = (async () => {
      const entry = this.index.chunks[i], response = await fetch(new URL(entry.file, this.base));
      if (!response.ok) throw new Error('轨迹分块加载失败：' + entry.file);
      const bytes = await response.arrayBuffer(), view = new DataView(bytes);
      if (bytes.byteLength !== entry.count * this.index.record_bytes) throw new Error('轨迹分块长度不符');
      const tracks = this.ids.map(() => []);
      for (let offset = 0; offset < bytes.byteLength; offset += this.index.record_bytes) {
        const row = { time: view.getFloat64(offset, true), edgeIndex: view.getUint32(offset + 12, true),
          state: view.getUint32(offset + 16, true), x: view.getFloat64(offset + 20, true),
          y: view.getFloat64(offset + 28, true), speed: view.getFloat64(offset + 36, true),
          distance_m: view.getFloat64(offset + 44, true), position_m: view.getFloat64(offset + 52, true) };
        tracks[view.getUint32(offset + 8, true)].push(row);
      }
      this.cache.set(i, tracks); this.tracksIndex = -1;
    })();
    this.pending.set(i, promise);
    try { await promise; } finally { this.pending.delete(i); }
  }
  ready(time) {
    const i = this.chunkIndex(time);
    return this.cache.has(i) && (i + 1 === this.index.chunks.length || this.cache.has(i + 1));
  }
  async ensure(time) {
    const i = this.chunkIndex(time);
    await Promise.all([this.load(i), this.load(i + 1)]);
    for (const key of this.cache.keys()) if (key < i - 1 || key > i + 1) this.cache.delete(key);
    this.assemble(i);
  }
  assemble(i) {
    if (i === this.tracksIndex) return;
    this.tracks = this.ids.map((_, car) => {
      const rows = [...(this.cache.get(i)?.[car] || []), ...(this.cache.get(i + 1)?.[car] || [])];
      rows.sort((a, b) => a.time - b.time);
      return rows.filter((row, n) => n === 0 || row.time !== rows[n - 1].time);
    });
    this.tracksIndex = i;
  }
  point(edge, position) {
    let remaining = Math.max(0, Math.min(edge.length, position));
    for (let i = 1; i < edge.shape.length; i++) {
      const a = edge.shape[i - 1], b = edge.shape[i], length = Math.hypot(b[0] - a[0], b[1] - a[1]);
      if (remaining <= length || i === edge.shape.length - 1) {
        const u = length ? Math.min(1, remaining / length) : 0;
        return [a[0] + u * (b[0] - a[0]), a[1] + u * (b[1] - a[1])];
      }
      remaining -= length;
    }
    return edge.shape.at(-1);
  }
  sample(rows, time, id) {
    let low = 0, high = rows.length;
    while (low < high) { const mid = (low + high) >> 1;
      if (rows[mid].time <= time + 1e-8) low = mid + 1; else high = mid;
    }
    const a = rows[Math.max(0, low - 1)], b = rows[low];
    if (!a) return null;
    let result = { ...a, id, edge: this.edges[a.edgeIndex].id, ...this.index.states[a.state] };
    if (!b || b.time - a.time > this.index.interval_s * 1.1 + 1e-7 || b.time <= a.time || time < a.time) return result;
    const ds = b.distance_m - a.distance_m;
    if (ds <= 1e-9) return result;
    const dt = b.time - a.time, u = Math.min(1, (time - a.time) / dt);
    // 只在相邻实测点之间平滑累计距离，并限制在已观测区间。
    const travel = Math.max(0, Math.min(ds,
      (-2*u*u*u + 3*u*u)*ds + (u*u*u - 2*u*u + u)*dt*a.speed + (u*u*u - u*u)*dt*b.speed));
    let edge = this.edges[a.edgeIndex], position = a.position_m + travel;
    if (a.edgeIndex !== b.edgeIndex) {
      const next = this.edges[b.edgeIndex];
      if (edge.to !== next.from) return result;
      if (position > edge.length) { position -= edge.length; edge = next; }
    }
    const [x, y] = this.point(edge, position);
    return { ...result, x, y, edge: edge.id, position_m: position,
      distance_m: a.distance_m + travel, speed: a.speed + (b.speed - a.speed)*u };
  }
  at(time) {
    const i = this.chunkIndex(time);
    for (const key of this.cache.keys()) if (key < i - 1 || key > i + 2) this.cache.delete(key);
    this.assemble(i);
    return this.tracks.map((rows, i) => this.sample(rows, time, this.ids[i])).filter(Boolean);
  }
  prefetch(time) {
    const i = this.chunkIndex(time), next = this.index.chunks[i + 1];
    if (next && next.start - time < 30) this.load(i + 2).catch(() => {});
  }
}
