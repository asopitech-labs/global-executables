# Changelog

## Unreleased

- ConanCenter: the recipe catalogue is re-read every seven days once the walk is
  complete, and a changed catalogue rolls over (new recipes and versions are queued in
  `catalog_pending`) instead of failing with "catalog changed". The catalogue had not
  been re-read since 2026-09-14 and was missing 3 recipes and 78 version updates (#58).
- vcpkg: CMake comments are ignored when reading `vcpkg_copy_tools(TOOL_NAMES ...)`.
  Commented names were published as commands (`mnn`: `cpp`, `converter`, `train`, ...;
  `openexr`: `not`, `exrcheck`). Re-parsed vcpkg and xmake packages now replace their
  older rows instead of accumulating them, so the next scheduled crawl removes those
  rows from `artifact-data` (#58).
- Moved the generated dictionary to the orphan `dictionary` branch. Consumers
  of `raw.githubusercontent.com/.../main/data/...` must change the branch segment
  to `dictionary`; raw GitHub URLs do not redirect across this cutover.
