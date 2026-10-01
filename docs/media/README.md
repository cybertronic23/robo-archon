# Physical task demonstration media

These are rendered MuJoCo `pick_place.v1` executions (seed 1), not generated illustrations. The locally generated arm videos use 1280×720, ~20 seconds; checked-in GIF previews are reduced to 640px / 12fps. The red cube moves through physical contact and friction; the green tray is the placement goal.

```bash
mkdir -p tmp-episodes
export ROBO_ARCHON_PYTHON="$PWD/.venv-mujoco/bin/python"
target/debug/robo-archon --robot franka_panda --backend mujoco --demo pick-place --seed 1 --save-frames false --record-video tmp-episodes/franka-panda-pick-place.mp4
target/debug/robo-archon --robot so101 --backend mujoco --demo pick-place --seed 1 --save-frames false --record-video tmp-episodes/so101-pick-place.mp4
ffmpeg -i tmp-episodes/franka-panda-pick-place.mp4 -filter_complex 'fps=12,scale=640:-1:flags=lanczos,split[a][b];[a]palettegen[p];[b][p]paletteuse' docs/media/franka-panda-pick-place.gif
```

Robot model attribution: [Franka Panda / MuJoCo Menagerie](https://github.com/google-deepmind/mujoco_menagerie/tree/4d038b3feae26ec82b46a4d586379114012a8ac7/franka_emika_panda), [SO101 / TheRobotStudio](https://github.com/TheRobotStudio/SO-ARM100/tree/5f6d2b876a53a4872e405b991dd925556c9e38a4/Simulation/SO101). Upstream assets and their LICENSE files are installed separately; the mesh data is not embedded in this repository. SO101 uses task-specific fingertip collision pads, as documented in the spec.

GitHub CI exports fresh MP4 and full Episode artifacts; the checked-in GIFs are previews of locally inspected acceptance examples. Verification fingerprints and measured task results are in [m2f-2-report.json](../validation/m2f-2-report.json).

## Repository media policy

Commit only compact GIF previews when animation is needed. Do not commit MP4, MOV, or WebM files; keep full recordings in ignored local output directories or CI artifacts. GIF is not inherently smaller than MP4: reduce dimensions, frame rate, duration, and palette, then check the actual file size before committing. Aim for at most 1 MiB per new GIF preview. Existing arm GIFs predate this target.

The Microduck official-policy probe preview is 280×210 at 6 fps, with 32 palette colors, covering the full 16-second rollout. See [the reproduction spec](../specs/m2f-3-microduck.md). Historical validation reports retain fingerprints of full local videos; those videos are no longer tracked in the current tree.
