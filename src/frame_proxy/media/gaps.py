from __future__ import annotations

from frame_proxy.media.avi import AviDescriptor
from frame_proxy.media.fingerprint import FrameFingerprint, fingerprint_frame
from frame_proxy.media.frames import decode_frame_at


def collect_fingerprints(
    media: bytes,
    descriptor: AviDescriptor,
    frame_indices: tuple[int, ...],
) -> tuple[FrameFingerprint, ...]:
    return tuple(
        fingerprint_frame(decode_frame_at(media, descriptor, frame_index))
        for frame_index in frame_indices
    )


def gap_frame_indices(
    sampled_frames: tuple[int, ...],
    coarse_pairs: tuple[tuple[int, int], ...],
) -> tuple[int, ...]:
    """Unsampled frames between each coarse previous sample and candidate.

    Coarse detection compared two adjacent sample positions. Precise
    localization scans every frame from the earlier sample through the
    candidate, so only frames the coarse pass never observed are returned;
    the sampled frames themselves are already fingerprinted.
    """
    sampled = set(sampled_frames)
    gaps: list[int] = []
    seen: set[int] = set()
    for previous_sample, candidate in coarse_pairs:
        for frame_index in range(previous_sample + 1, candidate + 1):
            if frame_index in sampled or frame_index in seen:
                continue
            seen.add(frame_index)
            gaps.append(frame_index)
    return tuple(gaps)
