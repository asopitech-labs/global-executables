from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_shared_go_pipeline_enforces_modern_go():
    pipeline = (ROOT / "tools/go_pipeline.sh").read_text()

    assert "modernization()" in pipeline
    assert "go fix -diff ./..." in pipeline
    assert pipeline.index("formatting\n  modernization") < pipeline.index("tests\n  security")
    assert "modern) environment; dependencies; formatting; modernization" in pipeline


def test_go_development_contract_uses_the_versioned_jetbrains_guidelines():
    agents = (ROOT / "AGENTS.md").read_text()
    guide = (ROOT / "docs/GO_CRAWLER_DEVELOPMENT.md").read_text()

    for contents in (agents, guide):
        assert "github.com/JetBrains/go-modern-guidelines@v0.1.1" in contents
        assert "list --file-path" in contents
    assert "go fix -diff ./..." in guide
