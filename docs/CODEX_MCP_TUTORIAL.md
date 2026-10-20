# Codex に global-executables MCP を追加する

このチュートリアルでは、`global-executables` を Codex CLI の MCP サーバー
として登録し、実際のツール呼び出しまで確認します。MCP サーバーは読み取り
専用で、API キーやリポジトリの clone は必要ありません。

## 前提

- Codex CLI がインストールされ、`codex --version` が実行できること
- [`uv`](https://docs.astral.sh/uv/getting-started/installation/) がインストール
  され、`uvx --version` が実行できること

## 1. Codex に MCP を追加する

次の1コマンドで登録します。

```sh
codex mcp add global-executables -- uvx global-executables mcp
```

初回起動時に `uvx` が PyPI からパッケージを取得します。公開データセットは
クエリに必要なファイルだけが取得され、ユーザーのキャッシュディレクトリに
保存されます。

## 2. 登録状態を確認する

一覧と個別設定を確認します。

```sh
codex mcp list
codex mcp get global-executables
```

次の内容になっていれば登録成功です。

```text
global-executables
  enabled: true
  transport: stdio
```

`codex mcp get` で、コマンドが `uvx`、引数が
`global-executables mcp` になっていることも確認してください。

## 3. Codex を再起動する

MCP の設定を追加・変更した後は、Codex CLI の新しいセッション、または
Codex アプリの再起動を行います。実行中のセッションが起動時に読み込んだ
MCP ツール一覧は、設定変更だけでは更新されない場合があります。

## 4. Codex から実際に確認する

新しいセッションで、次のように依頼します。

```text
global-executables MCP の check_executables を使って、
envcp と evpk の存在を確認してください。
```

確認対象の MCP ツールは次のとおりです。

- `check_executable`
- `check_executables`
- `get_executable`
- `search_executables`
- `search_similar_executables`
- `get_coverage`
- `assess_executable`
- `assess_executables`
- `resolve_installation`
- `resolve_installations`

現在のスナップショットでは、通常は次のような結果になります。スナップ
ショット更新後は結果が変わる可能性があります。

```text
envcp: found=true, status=collision
evpk:  found=false, status=unknown
```

`found=false` でも、部分収録のソースがある場合は `status=unknown` です。
これは「現在のインデックスにはない」ことと「すべての対象ソースに存在し
ない」ことを区別するための仕様です。レスポンスの `warnings`、
`absence.confidence`、`coverage_scope` も一緒に確認してください。

## 開発用チェックアウトを使う場合（任意）

サーバー自体を開発するときや、`dictionary` ブランチのローカルコピーを
使うときだけ、以下の構成が必要です。通常の利用ではこの手順を省略できます。

```sh
git clone https://github.com/asopitech-labs/global-executables.git
cd global-executables
export GLOBAL_EXECUTABLES_DIR="$PWD"
git fetch origin dictionary
git worktree add .dictionary origin/dictionary
python -m venv .venv
. .venv/bin/activate
python -m pip install -e .
```

すでにゼロセットアップ版を登録している場合は、いったん削除してから
ローカル版を登録します。

```sh
codex mcp remove global-executables
codex mcp add global-executables -- \
  "$GLOBAL_EXECUTABLES_DIR/.venv/bin/global-executables-mcp" \
  --root "$GLOBAL_EXECUTABLES_DIR" \
  --dataset-root "$GLOBAL_EXECUTABLES_DIR/.dictionary"
```

インストール後にサーバーだけを起動して確認する場合は、次を実行します。

```sh
global-executables-mcp \
  --root "$GLOBAL_EXECUTABLES_DIR" \
  --dataset-root "$GLOBAL_EXECUTABLES_DIR/.dictionary"
```

stdio サーバーは起動すると MCP 通信を待ち受けます。確認後は `Ctrl-C` で
終了してください。

## Codex を使わずに MCP プロトコルを確認する

Codex の設定確認とは独立して、リポジトリに含まれるコンテナテストでも
MCP の接続を確認できます。

```sh
tools/test_container_mcp.sh
```

このテストは、MCP サーバーをコンテナで起動し、別コンテナの Codex CLI に
登録してから、health endpoint、ツール、resource を確認します。モデルの
API リクエストや OpenAI ログインは必要ありません。

## トラブルシューティング

### `uvx` が見つからない

新しいターミナルで次を確認します。

```sh
uvx --version
```

実行できなければ `uv` をインストールし、Codex を再起動してください。
Codex からだけ見つからない場合は、`command -v uvx` で表示される絶対パスを
登録コマンドの `uvx` の代わりに指定できます。

### 設定を最初から登録し直す

登録内容が意図したものと異なる場合は、削除してから再登録します。

```sh
codex mcp remove global-executables
codex mcp add global-executables -- uvx global-executables mcp
```

### MCP ツールが Codex に表示されない

Codex の新しいセッションを開始するか、Codex アプリを再起動します。その
後、`codex mcp list` で `enabled` になっていることと、`codex mcp get
global-executables` のコマンドと引数が正しいことを確認します。

### `data/metadata.json` がないというエラーになる

このエラーは通常、開発用チェックアウトの構成で発生します。
`--dataset-root` が `dictionary` ブランチを materialize した `.dictionary`
を指しているか確認してください。`--root` はプログラムと schema のある
`main` チェックアウトを指します。2つの root を入れ替えないでください。

### 登録を解除する

```sh
codex mcp remove global-executables
```
