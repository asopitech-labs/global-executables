vcpkg_from_github(
    OUT_SOURCE_PATH SOURCE_PATH
    REPO example/toolkit
    REF v1.4.0
)
vcpkg_cmake_install()
vcpkg_copy_tools(
    TOOL_NAMES toolkit-run toolkit-dump ${EXTRA_TOOL}
    AUTO_CLEAN
)
