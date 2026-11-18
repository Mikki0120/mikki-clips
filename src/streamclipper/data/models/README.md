# Bundled models

## blaze_face_full_range.tflite

- What: MediaPipe BlazeFace (full range) face detector, float16, for the MediaPipe Tasks `FaceDetector`.
  StreamClipper runs it on the whole frame plus a 3x2 grid of overlapping tiles (`streamclipper.faces`), so
  small faces (a facecam in a corner, a wide podcast shot) are found too.
- Why full range: on a corner facecam (face ~8% of the frame height) it found the face on 14 of 20 test
  frames with tiles; the short-range model found it on 0 of 20.
- Source: https://storage.googleapis.com/mediapipe-models/face_detector/blaze_face_full_range/float16/1/blaze_face_full_range.tflite
- SHA-256: `3698b18f063835bc609069ef052228fbe86d9c9a6dc8dcb7c7c2d69aed2b181b`
- Size: 1,083,786 bytes
- Licence: Apache License 2.0 (Google MediaPipe models). Apache-2.0 is compatible with StreamClipper's
  AGPL-3.0-or-later.
- Used by: `streamclipper.faces.MediaPipeFaces` (needs the `vision` extra: `pip install -e ".[vision]"`).
  Without mediapipe, the OpenCV Haar cascades are used (`vision-haar` extra), and without either the
  layouts fall back to the skin-colour heuristic.
