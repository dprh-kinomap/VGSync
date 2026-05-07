#!/usr/bin/env python3
"""
video_prepass_full.py

Pre-pass for geolocating a video with sparse anchors:
- Detects cuts and "jumps" (segment boundaries)
- Estimates turn proxy (rotation between frames)
- Estimates speed proxy (relative speed changes) from optical flow magnitude
- Picks anchor timestamps (segment start/end + periodic + turn peaks)
- Exports frames for anchors and boundaries (and optional sequences around anchors)
- Outputs prepass.json containing metrics, segments, anchors

Dependencies:
  pip install opencv-python numpy

Usage:
  python video_prepass_full.py --video "front.mp4" --out "prepass_out" --export-seq
"""

import os
import json
import argparse
import shutil
import sys
import time
from dataclasses import dataclass, asdict
from typing import List, Dict, Any, Tuple, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed
import multiprocessing as mp

import cv2
import numpy as np


# -----------------------------
# Data structures
# -----------------------------

@dataclass
class FrameMetrics:
    t: float
    hist_diff: float
    speed_proxy: float
    rot_deg: float
    flow_inlier_ratio: float
    blur: float


@dataclass
class Segment:
    start: float
    end: float
    reason: str  # "cut", "jump", "eof"


# -----------------------------
# Utility functions
# -----------------------------

def hsv_hist(frame_bgr: np.ndarray, bins=(8, 8, 8)) -> np.ndarray:
    hsv = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2HSV)
    hist = cv2.calcHist([hsv], [0, 1, 2], None, bins, [0, 256, 0, 256, 0, 256])
    cv2.normalize(hist, hist, alpha=0, beta=1, norm_type=cv2.NORM_MINMAX)
    return hist


def blur_score(gray: np.ndarray) -> float:
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


def resize_for_flow(gray: np.ndarray, max_w: int = 640) -> np.ndarray:
    h, w = gray.shape[:2]
    if w <= max_w:
        return gray
    scale = max_w / float(w)
    return cv2.resize(gray, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)


def estimate_motion(prev_gray: np.ndarray, curr_gray: np.ndarray, flow_max_w: int = 640) -> Tuple[float, float, float]:
    prev = resize_for_flow(prev_gray, max_w=flow_max_w)
    curr = resize_for_flow(curr_gray, max_w=flow_max_w)

    h, w = prev.shape[:2]

    mask = np.zeros_like(prev, dtype=np.uint8)
    mask[int(h * 0.4):, :] = 255

    p0 = cv2.goodFeaturesToTrack(prev, maxCorners=900, qualityLevel=0.01, minDistance=8, mask=mask)
    if p0 is None or len(p0) < 25:
        return 0.0, 0.0, 0.0

    p1, st, err = cv2.calcOpticalFlowPyrLK(
        prev, curr, p0, None,
        winSize=(21, 21),
        maxLevel=3,
        criteria=(cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 30, 0.01),
    )
    if p1 is None or st is None:
        return 0.0, 0.0, 0.0

    st = st.reshape(-1)
    good0 = p0[st == 1].reshape(-1, 2)
    good1 = p1[st == 1].reshape(-1, 2)

    if len(good0) < 25:
        return 0.0, 0.0, 0.0

    flow = good1 - good0
    mags = np.linalg.norm(flow, axis=1)

    bottom = good0[:, 1] > (h * 0.4)
    mags_bottom = mags[bottom] if np.any(bottom) else mags
    speed_proxy = float(np.median(mags_bottom)) if len(mags_bottom) else 0.0

    M, inliers = cv2.estimateAffinePartial2D(
        good0, good1,
        method=cv2.RANSAC,
        ransacReprojThreshold=3.0,
        maxIters=2000,
        confidence=0.99,
    )
    if M is None or inliers is None:
        return speed_proxy, 0.0, 0.0

    inlier_ratio = float(np.mean(inliers))

    a, b = M[0, 0], M[0, 1]
    rot_rad = float(np.arctan2(b, a))
    rot_deg = float(np.degrees(rot_rad))

    return speed_proxy, rot_deg, inlier_ratio


def smooth_ma(x: np.ndarray, win: int = 11) -> np.ndarray:
    if win <= 1 or len(x) < 3:
        return x
    if win % 2 == 0:
        win += 1
    win = min(win, len(x) if len(x) % 2 == 1 else len(x) - 1)
    if win < 3:
        return x
    k = np.ones(win, dtype=np.float32) / float(win)
    return np.convolve(x, k, mode="same")


def ensure_dir(p: str):
    os.makedirs(p, exist_ok=True)


def resize_keep_aspect(bgr: np.ndarray, width: int) -> np.ndarray:
    h, w = bgr.shape[:2]
    if w <= width:
        return bgr
    scale = width / float(w)
    return cv2.resize(bgr, (width, int(h * scale)), interpolation=cv2.INTER_AREA)


def fmt_time(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    if h > 0:
        return f"{h:d}:{m:02d}:{s:02d}"
    return f"{m:d}:{s:02d}"


def progress_line(prefix: str, cur: int, total: int, t_video: float, extra: str = ""):
    if total > 0:
        pct = (cur / total) * 100.0
        msg = f"{prefix} {cur}/{total} ({pct:5.1f}%)  video@{fmt_time(t_video)} {extra}"
    else:
        msg = f"{prefix} {cur}  video@{fmt_time(t_video)} {extra}"
    sys.stdout.write("\r" + msg[:120].ljust(120))
    sys.stdout.flush()


# -----------------------------
# Core analysis
# -----------------------------

def analyze_video(
    video_path: str,
    sample_fps: float,
    hist_cut_thresh: float,
    jump_hist_thresh: float,
    jump_inlier_thresh: float,
    min_segment_len_s: float,
    flow_max_w: int,
    speed_spike_thresh: float = 50.0,
    rot_spike_thresh: float = 0.50,
    boundary_debug_dir: Optional[str] = None,
    progress_every: int = 50,
) -> Tuple[float, List[FrameMetrics], List[Segment], List[Tuple[str, float]]]:
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise RuntimeError(f"Cannot open video: {video_path}")

    fps = cap.get(cv2.CAP_PROP_FPS)
    if fps is None or fps <= 0:
        fps = 30.0

    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 0
    frame_step = max(1, int(round(fps / max(sample_fps, 0.1))))
    total_analyzed = (total_frames + frame_step - 1) // frame_step if total_frames > 0 else 0

    if boundary_debug_dir:
        ensure_dir(boundary_debug_dir)

    metrics: List[FrameMetrics] = []
    segments: List[Segment] = []
    boundary_times: List[Tuple[str, float]] = []

    prev_gray = None
    prev_hist = None
    seg_start_t = 0.0

    frame_idx = 0
    analyzed_idx = 0
    t0 = time.time()

    while True:
        ok, frame = cap.read()
        if not ok:
            break

        if frame_idx % frame_step != 0:
            frame_idx += 1
            continue

        t = frame_idx / fps
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        b = blur_score(gray)

        if prev_gray is None:
            prev_gray = gray
            prev_hist = hsv_hist(frame)
            metrics.append(FrameMetrics(t=t, hist_diff=0.0, speed_proxy=0.0, rot_deg=0.0, flow_inlier_ratio=1.0, blur=b))
        else:
            curr_hist = hsv_hist(frame)
            hist_diff = float(cv2.compareHist(prev_hist, curr_hist, cv2.HISTCMP_BHATTACHARYYA))

            speed_proxy, rot_deg, inlier_ratio = estimate_motion(prev_gray, gray, flow_max_w=flow_max_w)

            metrics.append(FrameMetrics(
                t=t,
                hist_diff=hist_diff,
                speed_proxy=speed_proxy,
                rot_deg=rot_deg,
                flow_inlier_ratio=inlier_ratio,
                blur=b
            ))

            is_cut = hist_diff >= hist_cut_thresh
            is_jump = (hist_diff >= jump_hist_thresh and inlier_ratio <= jump_inlier_thresh)
            is_speed_spike = speed_proxy >= speed_spike_thresh
            is_rot_spike = abs(rot_deg) >= rot_spike_thresh
            is_motion_spike = is_speed_spike and is_rot_spike

            if is_cut or is_jump or is_motion_spike:
                if is_cut:
                    reason = "cut"
                elif is_jump:
                    reason = "jump"
                else:
                    reason = "motion"
                seg_end_t = t

                if (seg_end_t - seg_start_t) >= min_segment_len_s:
                    segments.append(Segment(start=seg_start_t, end=seg_end_t, reason=reason))
                    boundary_times.append((reason, seg_end_t))
                    seg_start_t = seg_end_t

                    if boundary_debug_dir:
                        outp = os.path.join(boundary_debug_dir, f"boundary_{reason}_t_{seg_end_t:010.2f}.jpg")
                        cv2.imwrite(outp, frame)

            prev_gray = gray
            prev_hist = curr_hist

        analyzed_idx += 1
        if analyzed_idx % progress_every == 0:
            elapsed = time.time() - t0
            extra = f"elapsed {fmt_time(elapsed)}"
            progress_line("Analyze:", analyzed_idx, total_analyzed, t, extra)

        frame_idx += 1

    sys.stdout.write("\n")

    end_t = (max(total_frames - 1, 0) / fps) if total_frames > 0 else (metrics[-1].t if metrics else 0.0)
    if end_t > seg_start_t + 0.1:
        segments.append(Segment(start=seg_start_t, end=end_t, reason="eof"))

    cap.release()
    return fps, metrics, segments, boundary_times


def rebuild_segments_from_metrics(
    metrics: List[FrameMetrics],
    hist_cut_thresh: float,
    jump_hist_thresh: float,
    jump_inlier_thresh: float,
    speed_spike_thresh: float,
    rot_spike_thresh: float,
    min_segment_len_s: float,
) -> Tuple[List[Segment], List[Tuple[str, float]]]:
    """Rebuild segments and boundary_times by re-applying detection logic to existing metrics."""
    segments: List[Segment] = []
    boundary_times: List[Tuple[str, float]] = []
    
    seg_start_t = 0.0
    
    for i in range(1, len(metrics)):
        m = metrics[i]
        t = m.t
        hist_diff = m.hist_diff
        speed_proxy = m.speed_proxy
        rot_deg = abs(m.rot_deg)
        inlier_ratio = m.flow_inlier_ratio
        
        is_cut = hist_diff >= hist_cut_thresh
        is_jump = (hist_diff >= jump_hist_thresh and inlier_ratio <= jump_inlier_thresh)
        is_speed_spike = speed_proxy >= speed_spike_thresh
        is_rot_spike = rot_deg >= rot_spike_thresh
        is_motion_spike = is_speed_spike and is_rot_spike
        
        if is_cut or is_jump or is_motion_spike:
            if is_cut:
                reason = "cut"
            elif is_jump:
                reason = "jump"
            else:
                reason = "motion"
            seg_end_t = t
            
            if (seg_end_t - seg_start_t) >= min_segment_len_s:
                segments.append(Segment(start=seg_start_t, end=seg_end_t, reason=reason))
                boundary_times.append((reason, seg_end_t))
                seg_start_t = seg_end_t
    
    # close last segment
    end_t = metrics[-1].t if metrics else 0.0
    if end_t > seg_start_t + 0.1:
        segments.append(Segment(start=seg_start_t, end=end_t, reason="eof"))
    
    return segments, boundary_times


def pick_anchors(
    metrics: List[FrameMetrics],
    segments: List[Segment],
    boundary_times: List[Tuple[str, float]],
    anchors_per_minute: float,
    extra_turn_anchors: int,
    min_anchor_gap_s: float,
    rot_peak_min: float,
    blur_min: float,
    inlier_min: float,
    min_gap_to_boundary_s: float = 1.0,
) -> List[float]:
    if not metrics:
        return []

    t = np.array([m.t for m in metrics], dtype=np.float32)
    rot = np.array([abs(m.rot_deg) for m in metrics], dtype=np.float32)
    inl = np.array([m.flow_inlier_ratio for m in metrics], dtype=np.float32)
    blur = np.array([m.blur for m in metrics], dtype=np.float32)

    rot = rot * (inl > inlier_min).astype(np.float32) * (blur > blur_min).astype(np.float32)
    rot_s = smooth_ma(rot, win=11)

    # Extract boundary timestamps
    boundary_set = {t for _, t in boundary_times}

    anchors: List[float] = []

    def push(a: float):
        if not anchors:
            anchors.append(a)
            return
        if abs(a - anchors[-1]) >= min_anchor_gap_s:
            anchors.append(a)

    def pick_turn_peaks(ts: float, te: float, k: int) -> List[float]:
        idx = np.where((t >= ts) & (t <= te))[0]
        if len(idx) < 10:
            return []
        rr = rot_s[idx]
        order = np.argsort(-rr)

        chosen: List[float] = []
        for oi in order:
            if rr[oi] < rot_peak_min:
                break
            tt = float(t[idx[oi]])
            if all(abs(tt - c) >= min_anchor_gap_s for c in chosen):
                chosen.append(tt)
                if len(chosen) >= k:
                    break
        return sorted(chosen)

    period = 60.0 / max(anchors_per_minute, 1e-6)

    for seg in segments:
        ts, te = float(seg.start), float(seg.end)
        push(ts)

        dur = te - ts
        if dur > max(2 * period, 15.0):
            p = ts + period
            while p < te:
                push(p)
                p += period

        for a in pick_turn_peaks(ts, te, extra_turn_anchors):
            push(a)

        push(te)

    anchors = sorted(set([round(a, 2) for a in anchors]))
    
    # Filter out anchors too close to detected boundaries
    filtered_anchors = [
        a for a in anchors
        if not any(abs(a - b) < min_gap_to_boundary_s for b in boundary_set)
    ]
    
    return filtered_anchors


def _extract_and_save_frame(video_path: str, timestamp: float, out_path: str, export_width: int) -> bool:
    """Extract frame at timestamp and save to file. Returns True if successful."""
    try:
        cap = cv2.VideoCapture(video_path)
        if not cap.isOpened():
            return False
        
        cap.set(cv2.CAP_PROP_POS_MSEC, float(timestamp) * 1000.0)
        ok, frame = cap.read()
        cap.release()
        
        if not ok:
            return False
        
        frame = resize_keep_aspect(frame, export_width)
        cv2.imwrite(out_path, frame)
        return True
    except Exception:
        return False


def export_frames(
    video_path: str,
    out_dir: str,
    anchor_times: List[float],
    boundary_times: List[Tuple[str, float]],
    export_seq: bool,
    seq_half_window_s: float,
    seq_fps: float,
    export_width: int,
    progress_every: int = 25,
    num_threads: int = 4,
):
    frames_root = os.path.join(out_dir, "frames")
    anchors_dir = os.path.join(frames_root, "anchors")
    bounds_dir  = os.path.join(frames_root, "boundaries")
    seq_dir     = os.path.join(frames_root, "seq")

    ensure_dir(anchors_dir)
    ensure_dir(bounds_dir)
    if export_seq:
        ensure_dir(seq_dir)

    # Build set of detected boundary times (to skip them from anchors)
    detected_boundary_times = {t for _, t in boundary_times}
    
    # anchors - only export periodic anchors, skip detected boundaries
    periodic_anchors = [t for t in anchor_times if t not in detected_boundary_times]
    
    # Prepare anchor tasks: (timestamp, output_path)
    anchor_tasks = []
    for t in periodic_anchors:
        out_path = os.path.join(anchors_dir, f"{t:010.2f}_anchor.jpg")
        anchor_tasks.append((t, out_path))
    
    # Prepare boundary tasks
    boundary_offsets = {
        "cut": (0.7, 0.7),
        "jump": (2.0, 2.0),
        "motion": (1.0, 1.0),
    }
    
    boundary_tasks = []
    for kind, t in boundary_times:
        pre_delta, post_delta = boundary_offsets.get(kind, (0.7, 0.7))
        for tag, tt in [("before", t - pre_delta), ("after", t + post_delta)]:
            if tt < 0:
                continue
            out_path = os.path.join(bounds_dir, f"{tt:010.2f}_{kind}_{tag}.jpg")
            boundary_tasks.append((tt, out_path))
    
    # Prepare sequence tasks (if needed)
    seq_tasks = []
    if export_seq:
        step = 1.0 / max(seq_fps, 0.1)
        for t in periodic_anchors:
            seq_sub = os.path.join(seq_dir, f"{t:010.2f}_seq")
            ensure_dir(seq_sub)
            tt = t - seq_half_window_s
            j = 0
            while tt <= t + seq_half_window_s + 1e-9:
                out_path = os.path.join(seq_sub, f"{tt:010.2f}_seq_{j:02d}.jpg")
                seq_tasks.append((tt, out_path))
                tt += step
                j += 1
    
    # Combine all tasks
    all_tasks = anchor_tasks + boundary_tasks + seq_tasks
    total_tasks = len(all_tasks)
    
    # Export frames with multithreading
    print(f"Exporting {total_tasks} frames with {num_threads} threads...")
    t0 = time.time()
    completed = 0
    
    with ThreadPoolExecutor(max_workers=num_threads) as executor:
        futures = {
            executor.submit(_extract_and_save_frame, video_path, t, path, export_width): (t, path)
            for t, path in all_tasks
        }
        
        for future in as_completed(futures):
            completed += 1
            if completed % progress_every == 0 or completed == total_tasks:
                elapsed = time.time() - t0
                extra = f"elapsed {fmt_time(elapsed)}"
                progress_line("Export frames:", completed, total_tasks, 0.0, extra)
    
    sys.stdout.write("\n")
    print(f"Exported {total_tasks} frames in {fmt_time(time.time() - t0)}")


def save_prepass(out_json: str, metrics: List[FrameMetrics], segments: List[Segment], anchors: List[float], extra: Dict[str, Any]):
    payload: Dict[str, Any] = {
        "metrics": [asdict(m) for m in metrics],
        "segments": [asdict(s) for s in segments],
        "anchors_seconds": anchors,
        "extra": extra,
    }
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    print(f"Wrote: {out_json}")


# -----------------------------
# CLI
# -----------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--video", default=None, help="Path to video file (required for initial analysis)")
    ap.add_argument("--out", default="prepass_out", help="Output folder")
    ap.add_argument("--sample-fps", type=float, default=3.0, help="Sampling FPS for analysis (2-5 typical)")

    ap.add_argument("--hist-cut", type=float, default=0.55, help="Histogram diff threshold for hard cut")
    ap.add_argument("--jump-hist", type=float, default=0.18, help="Histogram diff threshold for jump")
    ap.add_argument("--jump-inlier", type=float, default=0.18, help="Inlier ratio threshold for jump (lower => incoherent)")
    ap.add_argument("--min-seg-len", type=float, default=3.0, help="Minimum segment length (seconds)")

    ap.add_argument("--speed-spike", type=float, default=50.0, help="Speed proxy threshold for motion boundary (both speed AND rotation must spike)")
    ap.add_argument("--rot-spike", type=float, default=0.50, help="Rotation (deg) threshold for motion boundary (both speed AND rotation must spike)")

    ap.add_argument("--anchors-per-minute", type=float, default=3.0, help="Periodic anchors inside each segment")
    ap.add_argument("--turn-anchors", type=int, default=6, help="Extra turn anchors per segment")
    ap.add_argument("--min-anchor-gap", type=float, default=8.0, help="Minimum gap between anchors (seconds)")
    ap.add_argument("--rot-peak-min", type=float, default=0.40, help="Min smoothed rotation(deg) to qualify as turn anchor")
    ap.add_argument("--blur-min", type=float, default=30.0, help="Min blur score to trust frame")
    ap.add_argument("--inlier-min", type=float, default=0.25, help="Min inlier ratio to trust motion/turn signal")

    ap.add_argument("--export-width", type=int, default=1024, help="Exported frame width (keep aspect)")
    ap.add_argument("--export-seq", action="store_true", help="Export a small sequence around each anchor")
    ap.add_argument("--seq-half-window", type=float, default=1.0, help="Seconds before/after anchor for sequence export")
    ap.add_argument("--seq-fps", type=float, default=2.0, help="FPS for sequence export around anchors")

    ap.add_argument("--flow-max-w", type=int, default=640, help="Resize width for optical-flow computation")
    ap.add_argument("--progress-every", type=int, default=50, help="Progress update frequency (analysis steps)")
    ap.add_argument("--num-threads", type=int, default=4, help="Number of threads for frame export (4 default)")

    args = ap.parse_args()

    ensure_dir(args.out)
    boundary_debug_dir = os.path.join(args.out, "debug_boundaries")

    # Detect retune mode: if no --video, load from existing prepass.json
    is_retune = args.video is None
    
    if is_retune:
        # Retune mode: load metrics from existing output folder
        prepass_json = os.path.join(args.out, "prepass.json")
        if not os.path.exists(prepass_json):
            raise RuntimeError(f"Retune mode requires existing {prepass_json}. Provide --video for initial analysis.")
        
        print(f"Retune mode: Loading metrics from {prepass_json}...")
        with open(prepass_json, 'r', encoding='utf-8') as f:
            data = json.load(f)
        metrics = [FrameMetrics(**m) for m in data['metrics']]
        fps = data['extra'].get('fps', 30.0)
        video_path = data['extra'].get('video')  # Extract video path from saved metadata
        
        # If video path not in JSON, need to run fresh analysis first
        if not video_path:
            raise RuntimeError(
                f"Video path not found in {prepass_json}.\n"
                f"Please run a fresh analysis first:\n"
                f"  python video_prepass.py --video <path> --out {args.out}\n"
                f"Then retune will work with:\n"
                f"  python video_prepass.py --out {args.out} [--new-params]"
            )
        print(f"Loaded {len(metrics)} metric points from video: {video_path}")
        print(f"Loaded {len(metrics)} metric points from video: {video_path}")
        
        # Rebuild segments with new thresholds
        print("Rebuilding segments with new detection thresholds...")
        segments, boundary_times = rebuild_segments_from_metrics(
            metrics=metrics,
            hist_cut_thresh=args.hist_cut,
            jump_hist_thresh=args.jump_hist,
            jump_inlier_thresh=args.jump_inlier,
            speed_spike_thresh=args.speed_spike,
            rot_spike_thresh=args.rot_spike,
            min_segment_len_s=args.min_seg_len,
        )
        
        # Clean frames but keep metrics
        frames_dir = os.path.join(args.out, "frames")
        if os.path.exists(frames_dir):
            print(f"Cleaning frames folder: {frames_dir}")
            shutil.rmtree(frames_dir)
        ensure_dir(boundary_debug_dir)
    else:
        # Full analysis mode
        video_path = args.video
        if os.path.exists(args.out):
            print(f"Cleaning output folder: {args.out}")
            shutil.rmtree(args.out)
        
        os.makedirs(args.out, exist_ok=True)
        ensure_dir(boundary_debug_dir)
        
        print("Analyzing video (this is the longest step)...")
        fps, metrics, segments, boundary_times = analyze_video(
            video_path=args.video,
            sample_fps=args.sample_fps,
            hist_cut_thresh=args.hist_cut,
            jump_hist_thresh=args.jump_hist,
            jump_inlier_thresh=args.jump_inlier,
            min_segment_len_s=args.min_seg_len,
            flow_max_w=args.flow_max_w,
            speed_spike_thresh=args.speed_spike,
            rot_spike_thresh=args.rot_spike,
            boundary_debug_dir=boundary_debug_dir,
            progress_every=max(1, args.progress_every),
        )

    print(f"Video FPS: {fps:.3f}")
    print(f"Metrics points: {len(metrics)}")
    print(f"Segments: {len(segments)} (including eof)")
    print(f"Boundaries saved: {len(boundary_times)} -> {boundary_debug_dir}")

    print("Picking anchors...")
    anchors = pick_anchors(
        metrics=metrics,
        segments=segments,
        boundary_times=boundary_times,
        anchors_per_minute=args.anchors_per_minute,
        extra_turn_anchors=args.turn_anchors,
        min_anchor_gap_s=args.min_anchor_gap,
        rot_peak_min=args.rot_peak_min,
        blur_min=args.blur_min,
        inlier_min=args.inlier_min,
    )
    print(f"Anchors: {len(anchors)}")

    print("Exporting frames...")
    export_frames(
        video_path=video_path,
        out_dir=args.out,
        anchor_times=anchors,
        boundary_times=boundary_times,
        export_seq=args.export_seq,
        seq_half_window_s=args.seq_half_window,
        seq_fps=args.seq_fps,
        export_width=args.export_width,
        progress_every=25,
        num_threads=args.num_threads,
    )

    # Save/update prepass.json
    out_json = os.path.join(args.out, "prepass.json")
    extra = {
        "fps": fps,
        "sample_fps": args.sample_fps,
        "hist_cut_thresh": args.hist_cut,
        "jump_hist_thresh": args.jump_hist,
        "jump_inlier_thresh": args.jump_inlier,
        "flow_max_w": args.flow_max_w,
    }
    if args.video:
        extra["video"] = os.path.abspath(args.video)
    
    save_prepass(
        out_json=out_json,
        metrics=metrics,
        segments=segments,
        anchors=anchors,
        extra=extra
    )

    print("Done.")
    print(f"Frames: {os.path.join(args.out, 'frames')}")
    print("Next: run geo-matching only on frames/anchors (or frames/seq for sequence matching).")


if __name__ == "__main__":
    main()
