# Codex に global-executables MCP を追加する

このチュートリアルでは、ローカルの `global-executables` チェックアウトを
Codex CLI の MCP サーバーとして登録し、登録状態と実際のツール呼び出しを
確認します。MCP サーバーは読み取り専用です。

## 前提

- `global-executables` のチェックアウトがあること
- Codex CLI がインストールされ、`codex --version` が実行できること
- Python 3.11 以上があること

以下では、リポジトリの絶対パスを環境変数に入れて使います。

```sh
export GLOBAL_EXECUTABLES_DIR=/absolute/path/to/global-executables
cd "$GLOBAL_EXECUTABLES_DIR"
git fetch origin dictionary
git worktree add .dictionary origin/dictionary
```

## 1. MCP サーバーを準備する

プロジェクトの仮想環境を作成し、MCP サーバーをインストールします。

```sh
python -m venv .venv
. .venv/bin/activate
python -m pip install -e .
```

インストール後、サーバーが起動できることを確認します。

```sh
global-executables-mcp \
  --root "$GLOBAL_EXECUTABLES_DIR" \
  --dataset-root "$GLOBAL_EXECUTABLES_DIR/.dictionary"
```

stdio サーバーは起動すると MCP 通信を待ち受けます。確認後は `Ctrl-C` で
終了してください。

## 2. Codex に MCP を追加する

インストールした実行ファイルを Codex に登録します。

```sh
codex mcp add global-executables -- \
  "$GLOBAL_EXECUTABLES_DIR/.venv/bin/global-executables-mcp" \
  --root "$GLOBAL_EXECUTABLES_DIR" \
  --dataset-root "$GLOBAL_EXECUTABLES_DIR/.dictionary"
```

`--` は、これ以降が Codex のオプションではなく MCP サーバーの起動
コマンドであることを示します。

仮想環境を作成せず、チェックアウトのソースを直接使う場合は、次の形式を
使えます。

```sh
codex mcp add global-executables \
  --env PYTHONPATH="$GLOBAL_EXECUTABLES_DIR/src" \
  -- python -m global_executables.mcp_server \
  --root "$GLOBAL_EXECUTABLES_DIR" \
  --dataset-root "$GLOBAL_EXECUTABLES_DIR/.dictionary"
```

## 3. 登録状態を確認する

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

`codex mcp get` で、コマンドが `global-executables-mcp` または
`python`、引数に `--root <リポジトリの絶対パス>` と
`--dataset-root <リポジトリの絶対パス>/.dictionary` が含まれていることも
確認してください。

## 4. Codex を再起動する

MCP の設定を追加・変更した後は、Codex CLI の新しいセッション、または
Codex アプリの再起動を行います。実行中のセッションが起動時に読み込んだ
MCP ツール一覧は、設定変更だけでは更新されない場合があります。

## 5. Codex から実際に確認する

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

現在のスナップショットでは、通常は次のような結果になります。スナップ
ショット更新後は結果が変わる可能性があります。

```text
envcp: found=true, status=collision
evpk:  found=false, status=unknown
```

`found=false` でも、部分収録のソースがある場合は `status=unknown` です。
これは「現在のインデックスにはない」ことと「すべての対象ソースに存在し
ない」ことを区別するための仕様です。レスポンスの
`absence.confidence` と `coverage_scope` も一緒に確認してください。

## 6. Codex を使わずに MCP プロトコルを確認する

Codex の設定確認とは独立して、リポジトリに含まれるコンテナテストでも
MCP の接続を確認できます。

```sh
tools/test_container_mcp.sh
```

このテストは、MCP サーバーをコンテナで起動し、別コンテナの Codex CLI に
登録してから、health endpoint、ツール、resource を確認します。モデルの
API リクエストや OpenAI ログインは必要ありません。

## トラブルシューティング

### `ModuleNotFoundError: global_executables`

ソースを直接使う形式では `PYTHONPATH` が必要です。まず値を確認します。

```sh
codex mcp get global-executables
```

`env` に `PYTHONPATH=<リポジトリ>/src` がなければ、いったん削除して追加し
直します。

```sh
codex mcp remove global-executables
```

### MCP ツールが Codex に表示されない

Codex の新しいセッションを開始するか、Codex アプリを再起動します。その
後、`codex mcp list` で `enabled` になっていることと、`--root` が正しい
ことを確認します。

### `data/metadata.json` がないというエラーになる

`--dataset-root` が `dictionary` ブランチを materialize した `.dictionary`
を指しているか確認してください。`--root` は program/schema のある `main`
チェックアウトを指します。2つの root を入れ替えないでください。

### 登録を解除する

```sh
codex mcp remove global-executables
```
