# Campus gate architecture

The Python server serves the compiled React sidebar application and an authenticated, CSRF-protected API. SQLite stores local users, vehicle registrations, observations, gate records and review history. There is no separate Node backend, Docker dependency or PostgreSQL requirement.

| Layer | Implementation |
|---|---|
| Inputs | Uploaded JPEG/PNG; server-connected USB/RTSP camera; local video replay |
| Shared recognition | `pipeline.recognize_observations` → `PlateEngine` → bundled Indian plate YOLO26s and PP-OCRv5 |
| Image handling | One RGB frame through shared recognition; human review; operator-declared direction/time |
| Video handling | YOLO26n vehicle detection and BoT-SORT tracking without appearance ReID; shared plate recognition; unambiguous containment assignment |
| Temporal decisions | Distinct-frame votes with source timestamps, agreement/confidence thresholds and minimum support span |
| Crossing decisions | Bottom-center positions across a calibrated finite line; hysteresis, stable side observations and gap reset |
| Records | Crossings staged before OCR finalization, preserving unreadable passages; merged records endpoint covers both inputs |
| Registration | Active local registration and optional validity interval, with event-time snapshots |
| Review | Optimistic version checks, original OCR retained, review notes and audit history |
| Duplicate controls | Idempotent same-track sequence insertion; suspicious nearby video records flagged; duplicate image submissions blocked across image/video records |
| UI | Scan (Live Video/Image Upload), Vehicle records, Vehicle registry, Settings |

A photograph cannot establish multi-frame consensus or direction. The adapters share recognition; temporal tracking is deliberately video-only. A video confidence score is not measured correctness. OCR confirmation can reinforce a consistent wrong reading.

Camera capture runs in a separate process from the image worker. One camera worker per data directory is enforced with a file lock. Live capture retains the latest frame and records dropped-frame counts; file replay preserves frame order. A stop request is checked between reads/frames. Shutdown has a bounded wait and can terminate a stuck worker, so session-recovery code marks incomplete crossings for review on restart.

The preview is a periodically refreshed processed image. It is not low-latency video streaming. File replay timestamps use the provided recording start time, or explicitly labeled replay start if none is known. No original capture date is invented from an uploaded image.

No campus trial, unattended barrier operation, parking management or real-time throughput guarantee is claimed. The application is a local gate registry prototype.
