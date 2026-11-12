"""Meshy Image-to-3D generation settings.

Every parameter, allowed value and range here was checked against the official
docs (https://docs.meshy.ai/en/api/image-to-3d and /en/api/pricing) on
2026-11-12. Deprecated/retired identifiers (lowpoly, meshy-t1, meshy-5,
meshy-7 for geometry) are intentionally not offered.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields

MODEL_TYPES = ("smart-topology", "standard")

# ai_model values allowed per model_type.
AI_MODELS = {
    "smart-topology": ("meshy-t2.5", "meshy-t2"),
    "standard": ("latest", "meshy-7.1", "meshy-6", "meshy-6-lite"),
}

AI_MODEL_LABELS = {
    "meshy-t2.5": "Meshy T2.5 - sharper detail for game assets (recommended)",
    "meshy-t2": "Meshy T2 - cleaner topology, natively separated parts",
    "latest": "Latest (currently Meshy 7.1)",
    "meshy-7.1": "Meshy 7.1",
    "meshy-6": "Meshy 6",
    "meshy-6-lite": "Meshy 6 Lite - lowest cost (2K textures only)",
}

# target_polycount ranges.
POLYCOUNT_RANGES = {
    "meshy-t2.5": (100, 30_000),
    "meshy-t2": (100, 15_000),
    "remesh": (100, 300_000),  # standard model with should_remesh=true
}

TEXTURE_RESOLUTIONS = ("2k", "4k", "8k")
EXPORT_FORMATS = ("fbx", "glb", "obj", "usdz", "stl", "3mf")
POSE_MODES = ("", "a-pose", "t-pose")
TOPOLOGIES = ("triangle", "quad")
ORIGINS = ("bottom", "center")
MAX_TEXTURE_PROMPT = 800

# Published credit prices per request (default texture model of each ai_model):
# (mesh only, 2K textures, 4K textures, 8K textures). None = not supported.
PRICING = {
    "latest": (20, 30, 30, 35),
    "meshy-7.1": (20, 30, 30, 35),
    "meshy-6": (20, 30, 30, 35),
    "meshy-6-lite": (5, 15, None, None),
    "meshy-t2": (5, 15, 15, 20),
    "meshy-t2.5": (5, 15, 15, 20),
}
PRICING_SOURCE = "Meshy published API pricing (docs.meshy.ai/en/api/pricing, checked 2026-11-12)"


@dataclass
class GenerationSettings:
    model_type: str = "smart-topology"
    ai_model: str = "meshy-t2.5"
    target_polycount: int = 10_000
    should_texture: bool = True
    enable_pbr: bool = False
    texture_resolution: str = "2k"
    texture_prompt: str = ""
    export_formats: list = field(default_factory=lambda: ["fbx"])
    # Only used with model_type="standard":
    should_remesh: bool = True
    topology: str = "triangle"
    # Optional extras:
    pose_mode: str = ""
    moderation: bool = False
    auto_size: bool = False
    origin_at: str = "bottom"

    # ------------------------------------------------------------------ io
    @classmethod
    def from_dict(cls, data: dict | None) -> "GenerationSettings":
        data = data or {}
        known = {f.name for f in fields(cls)}
        obj = cls(**{k: v for k, v in data.items() if k in known})
        obj.export_formats = list(obj.export_formats or [])
        obj.target_polycount = int(obj.target_polycount)
        return obj

    def to_dict(self) -> dict:
        return asdict(self)

    # ------------------------------------------------------------ helpers
    def polycount_range(self) -> tuple[int, int] | None:
        if self.model_type == "smart-topology":
            return POLYCOUNT_RANGES.get(self.ai_model)
        if self.should_remesh:
            return POLYCOUNT_RANGES["remesh"]
        return None  # polycount not used

    def estimate_credits(self) -> int | None:
        """Estimated credits from the published pricing table, or None if unknown."""
        table = PRICING.get(self.ai_model)
        if table is None:
            return None
        if not self.should_texture:
            return table[0]
        idx = {"2k": 1, "4k": 2, "8k": 3}.get(self.texture_resolution)
        return None if idx is None else table[idx]

    def validate(self) -> list[str]:
        errors: list[str] = []
        if self.model_type not in AI_MODELS:
            errors.append(f"Unknown model type '{self.model_type}'.")
            return errors
        if self.ai_model not in AI_MODELS[self.model_type]:
            errors.append(f"AI model '{self.ai_model}' is not valid for model type '{self.model_type}'.")
            return errors
        rng = self.polycount_range()
        if rng and not (rng[0] <= self.target_polycount <= rng[1]):
            errors.append(f"Target polycount must be between {rng[0]:,} and {rng[1]:,} for {self.ai_model}.")
        if self.should_texture:
            if self.texture_resolution not in TEXTURE_RESOLUTIONS:
                errors.append(f"Unknown texture resolution '{self.texture_resolution}'.")
            elif self.estimate_credits() is None:
                errors.append(f"{self.texture_resolution.upper()} textures are not supported by {self.ai_model}.")
            if len(self.texture_prompt) > MAX_TEXTURE_PROMPT:
                errors.append(f"Texture prompt must be at most {MAX_TEXTURE_PROMPT} characters.")
        if not self.export_formats:
            errors.append("Select at least one export format.")
        bad = [f for f in self.export_formats if f not in EXPORT_FORMATS]
        if bad:
            errors.append(f"Unsupported export format(s): {', '.join(bad)}.")
        if self.model_type == "standard" and self.should_remesh and self.topology not in TOPOLOGIES:
            errors.append(f"Unknown topology '{self.topology}'.")
        if self.pose_mode not in POSE_MODES:
            errors.append(f"Unknown pose mode '{self.pose_mode}'.")
        if self.origin_at not in ORIGINS:
            errors.append(f"Unknown origin '{self.origin_at}'.")
        return errors

    def to_payload(self, image_url: str) -> dict:
        """Build the POST /openapi/v1/image-to-3d body. Only sends parameters that
        apply to the chosen model so Meshy never rejects the combination."""
        p: dict = {
            "image_url": image_url,
            "model_type": self.model_type,
            "ai_model": self.ai_model,
            "should_texture": bool(self.should_texture),
            "target_formats": list(self.export_formats),
        }
        if self.model_type == "smart-topology":
            # smart-topology ignores should_remesh and accepts only triangle topology.
            p["target_polycount"] = int(self.target_polycount)
        else:
            p["should_remesh"] = bool(self.should_remesh)
            if self.should_remesh:
                p["topology"] = self.topology
                p["target_polycount"] = int(self.target_polycount)
        if self.should_texture:
            p["texture_resolution"] = self.texture_resolution
            p["enable_pbr"] = bool(self.enable_pbr)
            if self.texture_prompt.strip():
                p["texture_prompt"] = self.texture_prompt.strip()
        if self.pose_mode:
            p["pose_mode"] = self.pose_mode
        if self.moderation:
            p["moderation"] = True
        if self.auto_size:
            p["auto_size"] = True
            p["origin_at"] = self.origin_at
        return p

    def describe(self) -> str:
        parts = [self.ai_model]
        rng = self.polycount_range()
        if rng:
            parts.append(f"{self.target_polycount:,} polys")
        parts.append(f"{self.texture_resolution.upper()} textures" + (" + PBR" if self.enable_pbr else "")
                     if self.should_texture else "no textures")
        parts.append("/".join(f.upper() for f in self.export_formats))
        return ", ".join(parts)
