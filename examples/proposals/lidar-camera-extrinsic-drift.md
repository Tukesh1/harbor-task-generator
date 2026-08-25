# Task Proposal: lidar-camera-extrinsic-drift

**Domain:** Autonomous driving / Sensor fusion (Robotics)

## Overview

A LiDAR-camera fused perception stack on an experimental AV platform is producing 3D bounding boxes whose projection onto the camera image is consistently off by 10–25 pixels in a way that correlates with the platform's recent thermal history. The factory extrinsic calibration was last refreshed two months ago and the standard re-calibration target (a checkerboard rig) is not available — the platform is on a customer site and the operators want the projection error driven back below 3 pixels by end of week using only data captured on the route. The 3D detection itself is fine; the labels and depth are consistent with LiDAR. The error is purely in the LiDAR↔camera extrinsic transform.

The agent is given (a) a few hours of synchronized LiDAR + multi-camera + IMU + odometry logs collected on the customer's route, (b) the current (drifted) extrinsics, (c) the camera intrinsics (these were re-calibrated separately and are known good), (d) the OEM's calibration tool which the agent may use as-is or replace, and (e) the verifier's evaluation protocol — projection error on a held-out set of frames in which LiDAR points hitting clearly identifiable image edges (sign posts, lane-marking boundaries, the leading edge of parked vehicles) provide a ground-truth alignment signal.

## Instruction Crux

Produce a new LiDAR↔camera extrinsic matrix (and per-camera variants for the front-left, front-right, and rear cameras) such that on the verifier's held-out evaluation set, the median 2D reprojection error of LiDAR-derived 3D points onto the corresponding camera image at the labeled edges is under 3 pixels per camera, and the 95th-percentile error is under 6 pixels per camera. The agent must also produce a self-reported uncertainty for each extrinsic estimate that, when used to gate the OEM's downstream fusion pipeline, results in zero rejected frames on a clean validation segment (i.e., the agent's uncertainty estimate must not be systematically too pessimistic).

The agent is free to use any calibration method: target-less (mutual-information maximization, edge alignment, motion-based hand-eye), targeted (if it can identify any in-scene target on the route), or hybrid. The deliverable is an extrinsics YAML and an uncertainty file at fixed paths.

## Solution Crux

Two competing failure modes are present and the right diagnosis requires distinguishing them.

The first is a slow, monotonic drift in the LiDAR-to-camera rotation — predominantly a pitch error of ~0.4° accumulated since the factory calibration — driven by the LiDAR mount's thermal expansion. The right fix uses target-less motion-based calibration on the recorded log: estimate rotation by solving the hand-eye problem on segments where the vehicle turns through varying yaw rates (the IMU + visual odometry give the camera trajectory; the LiDAR + ICP give the LiDAR trajectory; rotation between them is hand-eye AX=XB), and refine translation by aligning LiDAR points to camera edges on stationary segments. The second is a time-synchronization drift: the LiDAR's PTP clock has slipped ~6 ms relative to the cameras, which on a moving platform produces a translation-direction-correlated reprojection error that *looks like* a translation calibration error. An agent that fixes only the rotation finds the median error around 8–10 pixels; an agent that fixes only the time sync finds it around 6–8 pixels; both together push under 3.

Wrong-but-tempting solutions: (a) running the OEM target-less tool as-is — it converges to the wrong extrinsic because it doesn't model the time sync error; (b) fitting a single global extrinsic by ICP on all points — averages out exactly the motion-direction signal that distinguishes time-sync error from translation error; (c) per-frame extrinsics estimation — fits noise and produces wild uncertainty, failing the uncertainty gate; (d) using only static segments — gives a clean rotation but the translation is unobservable from static data, leaving residual error. The correct approach reaches a clean joint estimate of rotation, translation, *and* time offset using the full log with appropriately weighted motion segments, and produces an uncertainty estimate that reflects the joint observability.

## Difficulty Crux

The difficulty is that LiDAR-camera calibration is a rich subfield with many techniques, and the diagnostic chain that says "this is two coupled problems (extrinsic drift + time sync), not one" is not obvious from the symptoms. The most common engineer instinct — re-run the calibration tool — converges to a wrong local minimum because the tool's cost function does not model timing. Recognizing time sync as a contributor requires looking at the error pattern across motion regimes (in static frames the time sync vanishes; in fast-yaw turns it dominates), and recognizing thermal drift as the rotation source requires the temperature history. Either alone leaves the median error well above the gate.

This is the kind of work senior calibration engineers do at AV programs and it routinely takes a junior engineer multiple weeks to get right when they haven't seen the failure mode before. The task is not hard from volume — there are only a few extrinsic numbers to estimate — but from the depth of joint estimation and the discriminative reasoning required.

## Verification Note

The verifier projects LiDAR points onto each camera using the agent's extrinsics on the held-out evaluation frames, identifies edge correspondences via a frozen edge-detection step, computes per-camera median and 95th-percentile 2D reprojection error, and asserts the bounds. Separately, it runs the OEM fusion pipeline against a clean validation segment using the agent's uncertainty estimates and asserts no frame is rejected as out-of-spec. The held-out frames, the edge-detection step, the fusion pipeline, and the validation segment are all pinned. Deterministic; no LLM judge.
