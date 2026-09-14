vcpkg_from_github(
    OUT_SOURCE_PATH SOURCE_PATH
    REPO example/headers
    REF v2.0.0
)
vcpkg_cmake_install()
vcpkg_cmake_config_fixup(PACKAGE_NAME headers)
