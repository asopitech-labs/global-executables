# Excerpt of microsoft/vcpkg ports/mnn/portfile.cmake (2026-10-08).
if("test" IN_LIST FEATURES)
    # no install(TARGETS) for the following binaries. check the buildtrees...
    # vcpkg_copy_tools(
    #     TOOL_NAMES run_test.out benchmark.out benchmarkExprModels.out # test/
    #     AUTO_CLEAN
    # )
endif()
if("tools" IN_LIST FEATURES)
    vcpkg_copy_tools(
        TOOL_NAMES MNNV2Basic.out mobilenetTest.out backendTest.out testModel.out testModelWithDescrisbe.out getPerformance.out checkInvalidValue.out timeProfile.out # tools/cpp
                   quantized.out # tools/quantization
                   classficationTopkEval.out # tools/evaluation
                   MNNDump2Json MNNConvert # tools/converter
                   transformer.out train.out dataTransformer.out runTrainDemo.out # tools/train
        AUTO_CLEAN
    )
    if(BUILD_SHARED)
        vcpkg_copy_tools(TOOL_NAMES TestConvertResult AUTO_CLEAN) # tools/converter
    endif()
endif()
