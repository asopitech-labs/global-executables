-- Excerpt of xmake-io/xmake-repo packages/m/meson/xmake.lua (2026-10-08).
package("meson")
    set_kind("binary")
    set_homepage("https://mesonbuild.com/")
    set_description("Fast and user friendly build system.")
    set_license("Apache-2.0")

    add_urls("https://github.com/mesonbuild/meson/releases/download/$(version)/meson-$(version).tar.gz",
             "https://github.com/mesonbuild/meson.git")

    add_versions("1.12.1", "ab0a6ca09f8ef70c564c8241fb5a23957886a0b53fb58412b5e07eaf07dba743")
    add_versions("1.12.0", "88afe0c20e52030218924ac37d0c81c59b4b5f3ae3752c8c6d7470c7d365886c")
    add_versions("1.11.1", "6788ae299979643f8d841bcaf64352558436cae45a0355148a3aeeccf7913866")
    add_versions("0.56.0", "291dd38ff1cd55fcfca8fc985181dd39be0d3e5826e5f0013bf867be40117213")
    add_versions("0.50.1", "f68f56d60c80a77df8fc08fa1016bc5831605d4717b622c96212573271e14ecc")
