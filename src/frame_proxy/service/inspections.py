from __future__ import annotations

import hashlib

from frame_proxy.config import Settings
from frame_proxy.domain.errors import (
    ApplicationError,
    ErrorCode,
    MediaValidationError,
)
from frame_proxy.domain.models import InspectionRecord
from frame_proxy.domain.policies import (
    InspectionRequest,
    validate_clip_id,
    validate_identifier,
)
from frame_proxy.domain.transitions import transition_inspection
from frame_proxy.media.avi import probe_avi
from frame_proxy.media.detector import (
    PRECISE_MODE,
    analyze_frames,
    detect_coarse_cuts,
)
from frame_proxy.media.fingerprint import FrameFingerprint
from frame_proxy.media.gaps import collect_fingerprints, gap_frame_indices
from frame_proxy.storage.repository import FileRepository


class InspectionService:
    def __init__(self, repository: FileRepository, settings: Settings) -> None:
        self._repository = repository
        self._settings = settings

    def create(
        self,
        clip_id: str,
        request: InspectionRequest,
    ) -> InspectionRecord:
        normalized_clip_id = validate_clip_id(clip_id)
        clip = self._repository.get_clip(normalized_clip_id)
        if clip.state != "registered":
            raise MediaValidationError(
                "clip is not ready for inspection",
                code=ErrorCode.INVALID_STATE,
            )

        record = InspectionRecord(
            inspection_id=request.inspection_id,
            clip_id=clip.clip_id,
            state="pending",
            sensitivity=request.sensitivity,
            sample_stride=request.sample_stride,
            min_scene_frames=request.min_scene_frames,
            boundary_mode=request.boundary_mode,
        )
        self._repository.create_inspection(record)

        transition_inspection(record, "scanning", reason="frame scan started")
        self._repository.update_inspection(record)
        try:
            media = self._repository.get_clip_media(clip.clip_id)
            if hashlib.sha256(media).hexdigest() != clip.digest:
                raise MediaValidationError(
                    "stored clip digest does not match its record",
                    code=ErrorCode.FRAME_DECODE,
                )
            descriptor = probe_avi(media)
            sampled_frames = tuple(
                range(0, descriptor.frame_count, request.sample_stride)
            )
            fingerprints = collect_fingerprints(media, descriptor, sampled_frames)
            frame_fingerprints: dict[int, FrameFingerprint] | None = None
            if request.boundary_mode == PRECISE_MODE:
                coarse_pairs = detect_coarse_cuts(
                    descriptor.frame_count,
                    sampled_frames,
                    fingerprints,
                    request.sensitivity,
                    request.min_scene_frames,
                )
                gap_frames = gap_frame_indices(sampled_frames, coarse_pairs)
                gap_values = collect_fingerprints(media, descriptor, gap_frames)
                frame_fingerprints = dict(zip(sampled_frames, fingerprints))
                frame_fingerprints.update(zip(gap_frames, gap_values))
            result = analyze_frames(
                descriptor.frame_count,
                sampled_frames,
                fingerprints,
                sensitivity=request.sensitivity,
                min_scene_frames=request.min_scene_frames,
                boundary_mode=request.boundary_mode,
                frame_fingerprints=frame_fingerprints,
            )
            record.boundary_mode = result.boundary_mode
            record.sampled_frames = list(result.sampled_frames)
            record.cut_frames = list(result.cut_frames)
            record.scenes = list(result.scenes)
            if result.boundary_mode == PRECISE_MODE:
                record.coarse_cut_frames = list(result.coarse_cut_frames)
                record.boundaries = list(result.boundaries)
            transition_inspection(record, "indexed", reason="scene ranges verified")
            self._repository.update_inspection(record)
            return record
        except ApplicationError as exc:
            self._mark_failed(record, exc.code.value, exc.message)
            raise
        except Exception as exc:
            self._mark_failed(record, ErrorCode.INTERNAL.value, str(exc))
            raise ApplicationError(
                ErrorCode.INTERNAL,
                "shot indexing failed",
                status=500,
            ) from exc

    def _mark_failed(
        self,
        record: InspectionRecord,
        code: str,
        message: str,
    ) -> None:
        record.error_code = code
        record.error_message = message
        if record.state in {"pending", "scanning"}:
            transition_inspection(record, "failed", reason=code)
        try:
            self._repository.update_inspection(record)
        except Exception:
            pass

    def get(self, inspection_id: str) -> InspectionRecord:
        normalized = validate_identifier(inspection_id, "inspection_id")
        return self._repository.get_inspection(normalized)
