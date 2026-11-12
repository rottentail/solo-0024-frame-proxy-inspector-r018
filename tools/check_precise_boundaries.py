from __future__ import annotations

import hashlib

from frame_proxy.media.avi_writer import build_crossfade_avi, build_sample_avi
from frame_proxy.media.ppm import parse_ppm_header
from tools.harness import RunningService, require, require_status, success


PRECISE_REQUEST = {
    "sensitivity": 0.55,
    "sample_stride": 2,
    "min_scene_frames": 2,
    "boundary_mode": "precise",
}


def main() -> int:
    with RunningService() as service:
        hard_response = service.upload_clip("clip-precise-a", build_sample_avi())
        require_status(hard_response, 201, "hard cut clip setup")
        fade_response = service.upload_clip("clip-precise-b", build_crossfade_avi())
        require_status(fade_response, 201, "crossfade clip setup")

        sampled = service.request_json(
            "POST",
            "/v1/clips/clip-precise-a/inspections",
            {
                "inspection_id": "inspection-precise-sampled",
                "sensitivity": 0.55,
                "sample_stride": 2,
                "min_scene_frames": 2,
            },
        )
        require_status(sampled, 201, "sampled inspection")
        sampled_record = sampled.json()["inspection"]
        require(
            "boundary_mode" not in sampled_record,
            "default inspections must keep their original sampled shape",
        )
        require(
            sampled_record["cut_frames"] == [6, 10, 14],
            f"original sampled cuts changed: {sampled_record['cut_frames']}",
        )

        precise = service.request_json(
            "POST",
            "/v1/clips/clip-precise-a/inspections",
            {"inspection_id": "inspection-precise-a", **PRECISE_REQUEST},
        )
        require_status(precise, 201, "precise inspection")
        record = precise.json()["inspection"]
        require(record["state"] == "indexed", "precise inspection did not index")
        require(record["boundary_mode"] == "precise", "boundary mode was not preserved")
        require(
            record["coarse_cut_frames"] == [6, 10, 14],
            f"coarse judgment changed: {record['coarse_cut_frames']}",
        )
        require(
            record["cut_frames"] == [5, 9, 14],
            f"localized cuts are wrong: {record['cut_frames']}",
        )
        require(
            record["certain_cut_frames"] == [5, 9, 14],
            "certain cuts disagree with localized boundaries",
        )
        require(
            record["provisional_cut_frames"] == [],
            "hard cuts must not leave provisional boundaries",
        )

        boundaries = record["boundaries"]
        require(len(boundaries) == 3, "one evidence record per coarse change expected")
        for boundary in boundaries:
            require(boundary["status"] == "localized", "hard cut was not localized")
            require(boundary["certain"] is True, "localized boundary is not certain")
            require(
                boundary["localized_frame"] == boundary["scene_start_frame"],
                "localized frame and scene start disagree",
            )
            require(
                boundary["previous_sample"] < boundary["localized_frame"]
                <= boundary["coarse_frame"],
                "localized frame is outside its sampling window",
            )
            require(
                boundary["significant_steps"] == 1,
                "hard cuts must show exactly one switch step",
            )

        discovery = record["discovery"]
        require(
            discovery["complete"] is False,
            "coarse sampling cannot claim complete discovery",
        )
        require(
            discovery["unobserved_changes_possible"] is True,
            "unobserved changes must stay possible",
        )
        require(
            discovery["certain_boundaries"] == 3
            and discovery["uncertain_boundaries"] == 0,
            "boundary counters disagree with evidence",
        )

        scenes = record["scenes"]
        require(scenes[0]["start_frame"] == 0, "first scene must start at zero")
        require(scenes[-1]["end_frame"] == 17, "last scene must reach frame count")
        for left, right in zip(scenes, scenes[1:]):
            require(
                left["end_frame"] == right["start_frame"],
                "precise scene ranges are not contiguous",
            )
        require(
            [item["representative_frame"] for item in scenes] == [2, 6, 10, 14],
            "representative frames do not follow localized scene midpoints",
        )

        stored = service.request("GET", "/v1/inspections/inspection-precise-a")
        require_status(stored, 200, "precise inspection lookup")
        stored_record = stored.json()["inspection"]
        require(
            stored_record["cut_frames"] == record["cut_frames"],
            "stored precise cuts changed after publication",
        )
        require(
            stored_record["boundaries"] == record["boundaries"],
            "stored boundary evidence changed after publication",
        )

        for selection, sheet_id in (
            ("first", "sheet-precise-first"),
            ("midpoint", "sheet-precise-midpoint"),
            ("stored", "sheet-precise-stored"),
        ):
            response = service.request_json(
                "POST",
                "/v1/inspections/inspection-precise-a/contact-sheets",
                {
                    "sheet_id": sheet_id,
                    "columns": 2,
                    "tile_width": 64,
                    "gutter": 4,
                    "selection": selection,
                    "background": "#20252c",
                },
            )
            require_status(response, 201, f"{selection} contact sheet")
            sheet = response.json()["contact_sheet"]
            require(sheet["state"] == "ready", "contact sheet did not become ready")
            expected = [
                _expected_frame(scene, selection) for scene in scenes
            ]
            require(
                [tile["source_frame"] for tile in sheet["tile_map"]] == expected,
                f"{selection} selection did not use the precise scene ranges",
            )
            artifact = service.request(
                "GET",
                f"/v1/contact-sheets/{sheet_id}/artifact",
            )
            require_status(artifact, 200, f"{selection} artifact fetch")
            header = parse_ppm_header(artifact.body)
            require(
                header.width == sheet["image_width"]
                and header.height == sheet["image_height"],
                "artifact dimensions disagree with the sheet record",
            )
            require(
                hashlib.sha256(artifact.body).hexdigest()
                == sheet["artifact_digest"],
                "artifact digest disagrees with the sheet record",
            )

        fade = service.request_json(
            "POST",
            "/v1/clips/clip-precise-b/inspections",
            {
                "inspection_id": "inspection-precise-fade",
                "sensitivity": 0.55,
                "sample_stride": 4,
                "min_scene_frames": 2,
                "boundary_mode": "precise",
            },
        )
        require_status(fade, 201, "crossfade inspection")
        fade_record = fade.json()["inspection"]
        require(
            len(fade_record["boundaries"]) == 1,
            "the detected gradual change must be reported once",
        )
        fade_boundary = fade_record["boundaries"][0]
        require(
            fade_boundary["status"] == "insufficient_evidence",
            "a continuous change must not claim an exact boundary",
        )
        require(
            fade_boundary["certain"] is False,
            "continuous change was presented as certain",
        )
        require(
            fade_boundary["localized_frame"] is None,
            "continuous change invented a localized frame",
        )
        require(
            fade_boundary["scene_start_frame"] == fade_boundary["coarse_frame"],
            "uncertain boundary must keep its coarse candidate",
        )
        require(
            fade_record["certain_cut_frames"] == [],
            "continuous change must not appear among certain cuts",
        )
        require(
            fade_record["provisional_cut_frames"]
            == [fade_boundary["scene_start_frame"]],
            "provisional cuts must expose the unlocalized boundary",
        )
        require(
            fade_record["discovery"]["uncertain_boundaries"] == 1,
            "uncertain boundary counter is wrong",
        )
        require(
            fade_record["discovery"]["complete"] is False,
            "gradual footage must not claim complete discovery",
        )
        for scene in fade_record["scenes"]:
            require(
                scene["start_frame"] <= scene["end_frame"],
                "fade scene range is inverted",
            )

        rejected = service.request_json(
            "POST",
            "/v1/clips/clip-precise-a/inspections",
            {
                "inspection_id": "inspection-precise-bad",
                "sample_stride": 2,
                "min_scene_frames": 2,
                "boundary_mode": "guess",
            },
        )
        require_status(rejected, 400, "unknown boundary mode rejection")
        require(
            rejected.json()["error"]["code"] == "invalid_request",
            "unknown boundary mode returned the wrong error code",
        )
        missing = service.request(
            "GET",
            "/v1/inspections/inspection-precise-bad",
        )
        require_status(missing, 404, "rejected request visibility")

    success(
        "localize-shot-boundaries",
        {
            "coarse_cuts": record["coarse_cut_frames"],
            "localized_cuts": record["cut_frames"],
            "fade_status": fade_boundary["status"],
            "scenes": len(scenes),
        },
    )
    return 0


def _expected_frame(scene: dict[str, int], selection: str) -> int:
    if selection == "first":
        return scene["start_frame"]
    if selection == "midpoint":
        return scene["start_frame"] + (
            scene["end_frame"] - scene["start_frame"]
        ) // 2
    return scene["representative_frame"]


if __name__ == "__main__":
    raise SystemExit(main())
