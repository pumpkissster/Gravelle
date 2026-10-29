# Bundled YuNet face detector

Gravelle uses this model only to locate faces when an administrator generates a model avatar. Detection runs locally on the CPU using the existing OpenCV runtime. The model is included in the application and is never downloaded at runtime. It does not identify people or add features to the image-search index.

- Upstream: [OpenCV Zoo](https://github.com/opencv/opencv_zoo/tree/f12e12798e8314f7c074a6656816c048dcc95b7a/models/face_detection_yunet).
- Revision: `f12e12798e8314f7c074a6656816c048dcc95b7a`.
- Model: `face_detection_yunet_2023mar.onnx`, unchanged upstream bytes, 232,589 bytes.
- SHA-256: `8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4`.
- License: MIT; the complete, unchanged upstream notice is in [LICENSE](LICENSE).

`provenance.json` records the pinned official download URLs, file size, and SHA-256 hashes. The detector validates the model before loading it. The source archive, Docker image, and both desktop bundles keep this directory together with its notice and provenance. Test images and development downloads are excluded from distribution.
