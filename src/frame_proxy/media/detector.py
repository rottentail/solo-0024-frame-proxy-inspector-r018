from __future__ import annotations

from dataclasses import dataclass

from frame_proxy.domain.errors import ErrorCode, MediaValidationError
from frame_proxy.domain.models import BoundaryEvidence, SceneRecord
from frame_proxy.media.fingerprint import FrameFingerprint, fingerprint_distance


SAMPLED_MODE = "sampled"
PRECISE_MODE = "precise"
BOUNDARY_MODES = (SAMPLED_MODE, PRECISE_MODE)

LOCALIZED = "localized"
INSUFFICIENT_EVIDENCE = "insufficient_evidence"

REASON_SINGLE_STEP = "single_step"
REASON_CONTINUOUS_CHANGE = "continuous_change"
REASON_MULTIPLE_STEPS = "multiple_steps"
REASON_NO_SIGNIFICANT_STEP = "no_single_step_above_threshold"
REASON_MINIMUM_SCENE_GUARD = "minimum_scene_guard"


@dataclass(frozen=True, slots=True)
class AnalysisResult:
    sampled_frames: tuple[int, ...]
    cut_frames: tuple[int, ...]
    scenes: tuple[SceneRecord, ...]
    boundary_mode: str = SAMPLED_MODE
    coarse_cut_frames: tuple[int, ...] = ()
    boundaries: tuple[BoundaryEvidence, ...] = ()


def _threshold(sensitivity: float) -> float:
    return 0.50 - sensitivity * 0.30


def detect_coarse_cuts(
    frame_count: int,
    sampled_frames: tuple[int, ...],
    fingerprints: tuple[FrameFingerprint, ...],
    sensitivity: float,
    min_scene_frames: int,
) -> tuple[tuple[int, int], ...]:
    """Return (previous_sample, candidate_sample) pairs for coarse cuts.

    This preserves the original sampled judgment unchanged: threshold
    crossings between adjacent sample positions, filtered by the minimum
    scene length. Precise mode keeps this exact discovery set and only
    resolves where inside each pair's window the picture actually changed.
    """
    if len(sampled_frames) != len(fingerprints) or not sampled_frames:
        raise MediaValidationError(
            "sampled frames and fingerprints must be non-empty and aligned",
            code=ErrorCode.FRAME_DECODE,
        )
    threshold = _threshold(sensitivity)
    pairs: list[tuple[int, int]] = []
    last_cut = 0
    for index in range(1, len(sampled_frames)):
        distance = fingerprint_distance(fingerprints[index - 1], fingerprints[index])
        candidate = sampled_frames[index]
        if distance < threshold:
            continue
        if candidate - last_cut < min_scene_frames:
            continue
        if frame_count - candidate < min_scene_frames:
            continue
        pairs.append((sampled_frames[index - 1], candidate))
        last_cut = candidate
    return tuple(pairs)


def _detect_cut_frames(
    frame_count: int,
    sampled_frames: tuple[int, ...],
    fingerprints: tuple[FrameFingerprint, ...],
    sensitivity: float,
    min_scene_frames: int,
) -> list[int]:
    return [
        candidate
        for _, candidate in detect_coarse_cuts(
            frame_count,
            sampled_frames,
            fingerprints,
            sensitivity,
            min_scene_frames,
        )
    ]


def _window_fingerprint(
    frame_fingerprints: dict[int, FrameFingerprint],
    frame_index: int,
) -> FrameFingerprint:
    try:
        return frame_fingerprints[frame_index]
    except KeyError:
        raise MediaValidationError(
            "precise boundary localization requires a fingerprint for every "
            "frame between adjacent samples",
            code=ErrorCode.FRAME_DECODE,
            details={"frame_index": frame_index},
        )


def _classify_window(
    steps: list[tuple[int, float]],
    window_distance: float,
    threshold: float,
) -> tuple[int | None, str, str]:
    """Resolve one sample window into an exact switch frame, if one exists.

    A certain boundary requires exactly one frame-to-frame step at or above
    the cut threshold; that single step is the picture switch. Anything else
    is reported as insufficient evidence instead of choosing a frame that
    merely happens to sit inside a continuous change. The reported step
    count is the raw number found in the window, so a window solve can see
    exactly what the decision was based on.
    """
    if window_distance < threshold:
        return None, INSUFFICIENT_EVIDENCE, REASON_NO_SIGNIFICANT_STEP
    if len(steps) == 1:
        return steps[0][0], LOCALIZED, REASON_SINGLE_STEP
    if len(steps) > 1:
        adjacent = any(
            steps[index][0] + 1 == steps[index + 1][0]
            for index in range(len(steps) - 1)
        )
        return (
            None,
            INSUFFICIENT_EVIDENCE,
            REASON_CONTINUOUS_CHANGE if adjacent else REASON_MULTIPLE_STEPS,
        )
    return None, INSUFFICIENT_EVIDENCE, REASON_NO_SIGNIFICANT_STEP


def _fits_minimum_scene(
    boundary_frame: int,
    last_cut: int,
    frame_count: int,
    min_scene_frames: int,
) -> bool:
    return (
        boundary_frame - last_cut >= min_scene_frames
        and frame_count - boundary_frame >= min_scene_frames
    )


def _localize_boundaries(
    frame_count: int,
    sampled_frames: tuple[int, ...],
    fingerprints: tuple[FrameFingerprint, ...],
    sensitivity: float,
    min_scene_frames: int,
    frame_fingerprints: dict[int, FrameFingerprint],
) -> tuple[list[int], tuple[BoundaryEvidence, ...]]:
    threshold = _threshold(sensitivity)
    cuts: list[int] = []
    evidence: list[BoundaryEvidence] = []
    last_cut = 0

    for boundary_index, (previous_sample, candidate) in enumerate(
        detect_coarse_cuts(
            frame_count,
            sampled_frames,
            fingerprints,
            sensitivity,
            min_scene_frames,
        )
    ):
        steps: list[tuple[int, float]] = []
        max_step = (previous_sample + 1, 0.0)
        for frame_index in range(previous_sample + 1, candidate + 1):
            distance = fingerprint_distance(
                _window_fingerprint(frame_fingerprints, frame_index - 1),
                _window_fingerprint(frame_fingerprints, frame_index),
            )
            if distance >= threshold:
                steps.append((frame_index, distance))
            if distance > max_step[1]:
                max_step = (frame_index, distance)
        window_distance = fingerprint_distance(
            _window_fingerprint(frame_fingerprints, previous_sample),
            _window_fingerprint(frame_fingerprints, candidate),
        )

        localized_frame, status, reason = _classify_window(
            steps,
            window_distance,
            threshold,
        )
        if localized_frame is not None and not _fits_minimum_scene(
            localized_frame,
            last_cut,
            frame_count,
            min_scene_frames,
        ):
            # The exact switch is real but would create a too-short scene.
            # It is not a usable exact boundary, so keep it uncertain.
            localized_frame = None
            status = INSUFFICIENT_EVIDENCE
            reason = REASON_MINIMUM_SCENE_GUARD

        if localized_frame is not None:
            scene_start_frame = localized_frame
        else:
            # Keep the coarse judgment as a provisional split, explicitly
            # labelled uncertain: the discovered change is not dropped, and
            # no exact frame is claimed inside a continuous change. The coarse
            # candidate already passed the same minimum-scene guard, so it is
            # always a usable provisional start.
            scene_start_frame = candidate
        cuts.append(scene_start_frame)
        last_cut = scene_start_frame
        evidence.append(
            BoundaryEvidence(
                index=boundary_index,
                coarse_frame=candidate,
                previous_sample=previous_sample,
                scene_start_frame=scene_start_frame,
                localized_frame=localized_frame,
                certain=localized_frame is not None,
                status=status,
                reason=reason,
                scanned_frames=candidate - previous_sample,
                significant_steps=len(steps),
                step_threshold=threshold,
                window_distance=window_distance,
                max_step_frame=max_step[0],
                max_step_distance=max_step[1],
            )
        )

    return cuts, tuple(evidence)


def _scene_records(
    frame_count: int,
    sampled_frames: tuple[int, ...],
    fingerprints: tuple[FrameFingerprint, ...],
    cuts: list[int],
) -> tuple[SceneRecord, ...]:
    starts = [0, *cuts]
    ends = [*cuts, frame_count]
    fingerprint_by_frame = dict(zip(sampled_frames, fingerprints))
    scenes: list[SceneRecord] = []

    for scene_index, (start, end) in enumerate(zip(starts, ends)):
        in_scene = [
            (frame_index, fingerprint_by_frame[frame_index])
            for frame_index in sampled_frames
            if start <= frame_index < end
        ]
        midpoint = start + (end - start) // 2
        if in_scene:
            representative_frame, _ = min(
                in_scene,
                key=lambda item: abs(item[0] - midpoint),
            )
            count = len(in_scene)
            mean_luma = sum(item[1].mean_luma for item in in_scene) / count
            contrast = sum(item[1].contrast for item in in_scene) / count
            dominant = (
                round(sum(item[1].mean_red for item in in_scene) / count),
                round(sum(item[1].mean_green for item in in_scene) / count),
                round(sum(item[1].mean_blue for item in in_scene) / count),
            )
        else:
            representative_frame = midpoint
            nearest_index = min(
                range(len(sampled_frames)),
                key=lambda index: abs(sampled_frames[index] - midpoint),
            )
            fingerprint = fingerprints[nearest_index]
            mean_luma = fingerprint.mean_luma
            contrast = fingerprint.contrast
            dominant = (
                round(fingerprint.mean_red),
                round(fingerprint.mean_green),
                round(fingerprint.mean_blue),
            )
        scenes.append(
            SceneRecord(
                index=scene_index,
                start_frame=start,
                end_frame=end,
                representative_frame=representative_frame,
                mean_luma=mean_luma,
                contrast=contrast,
                dominant_rgb=dominant,
            )
        )
    return tuple(scenes)


def _validate_scenes(frame_count: int, scenes: tuple[SceneRecord, ...]) -> None:
    if not scenes or scenes[0].start_frame != 0:
        raise MediaValidationError(
            "scene ranges must begin at frame zero",
            code=ErrorCode.FRAME_DECODE,
        )
    if scenes[-1].end_frame != frame_count:
        raise MediaValidationError(
            "scene ranges must end at the clip frame count",
            code=ErrorCode.FRAME_DECODE,
        )
    for left, right in zip(scenes, scenes[1:]):
        if left.end_frame != right.start_frame:
            raise MediaValidationError(
                "scene ranges must be contiguous",
                code=ErrorCode.FRAME_DECODE,
            )
        if right.start_frame <= left.start_frame:
            raise MediaValidationError(
                "scene ranges must be strictly increasing",
                code=ErrorCode.FRAME_DECODE,
            )


def analyze_frames(
    frame_count: int,
    sampled_frames: tuple[int, ...],
    fingerprints: tuple[FrameFingerprint, ...],
    *,
    sensitivity: float,
    min_scene_frames: int,
    boundary_mode: str = SAMPLED_MODE,
    frame_fingerprints: dict[int, FrameFingerprint] | None = None,
) -> AnalysisResult:
    coarse_cuts = _detect_cut_frames(
        frame_count,
        sampled_frames,
        fingerprints,
        sensitivity,
        min_scene_frames,
    )
    if boundary_mode == SAMPLED_MODE:
        cuts = coarse_cuts
        boundaries: tuple[BoundaryEvidence, ...] = ()
    elif boundary_mode == PRECISE_MODE:
        if frame_fingerprints is None:
            raise MediaValidationError(
                "precise boundary mode requires gap frame fingerprints",
                code=ErrorCode.FRAME_DECODE,
            )
        cuts, boundaries = _localize_boundaries(
            frame_count,
            sampled_frames,
            fingerprints,
            sensitivity,
            min_scene_frames,
            frame_fingerprints,
        )
    else:
        raise MediaValidationError(
            "boundary mode must be sampled or precise",
            code=ErrorCode.INVALID_REQUEST,
            details={"boundary_mode": boundary_mode},
        )

    scenes = _scene_records(frame_count, sampled_frames, fingerprints, cuts)
    _validate_scenes(frame_count, scenes)
    return AnalysisResult(
        sampled_frames=sampled_frames,
        cut_frames=tuple(cuts),
        scenes=scenes,
        boundary_mode=boundary_mode,
        coarse_cut_frames=tuple(coarse_cuts),
        boundaries=boundaries,
    )
