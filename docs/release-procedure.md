# リリース手順: PyPI Trusted Publishing

- 関連: `.github/workflows/publish.yml`, `docs/design/duckdb-notebook-html-export-design.md`
- 前提: 本リポジトリは PyPI Trusted Publisher(OIDC)経由で公開する。APIトークンは発行・保存しない。

## 1. 事前準備(初回のみ)

以下はリポジトリのリリース権限を持つ人が1回だけ行う設定であり、通常のリリース作業のたびに繰り返す必要はない。

### 1.1 GitHubリポジトリ側: Environments の作成

GitHubリポジトリの Settings → Environments で、以下の2つの environment を作成する。

- `testpypi`
- `pypi`

保護ルール(Required reviewers 等)は任意だが、`pypi` environment には承認者を設定しておくと、タグpush時に人手でのレビューを挟めるため推奨する。

### 1.2 PyPI側: Trusted Publisher の登録(本番)

1. https://pypi.org でアカウントにログインする。
2. 対象プロジェクト(初回はまだ存在しないため、アカウントの管理画面から「pending publisher」として登録する: https://pypi.org/manage/account/publishing/ )。
3. 以下の内容で登録する。

   | 項目 | 値 |
   |---|---|
   | PyPI Project Name | `duckdb-nb-export` |
   | Owner | `b-trout` |
   | Repository name | `duckdb-nb-export` |
   | Workflow name | `publish.yml` |
   | Environment name | `pypi` |

4. 登録後、初回のタグpushでこのワークフローが実行されると、pending publisher が正式なプロジェクトの Trusted Publisher に昇格する。

### 1.3 TestPyPI側: Trusted Publisher の登録(予行用)

同様に https://test.pypi.org でも登録する。値はほぼ同じだが、Environment name のみ異なる。

| 項目 | 値 |
|---|---|
| PyPI Project Name | `duckdb-nb-export` |
| Owner | `b-trout` |
| Repository name | `duckdb-nb-export` |
| Workflow name | `publish.yml` |
| Environment name | `testpypi` |

## 2. リリース手順(通常フロー)

1. **バージョンを上げる**: `src/duckdb_ui_notebook_export/__init__.py` の `__version__` を更新する。バージョン番号の単一ソースはここのみであり(`pyproject.toml` は `dynamic = ["version"]` でここを参照する)、他のファイルを個別に更新する必要はない。
2. **CHANGELOG.md を更新する**: `CHANGELOG.md` の `[Unreleased]` セクションを新バージョンの節に繰り下げ、日付を記入する。
3. **コミット & PRマージ**: 変更をコミットし、通常のPRフローで `main` にマージする。
4. **タグを作成してpush**する。

   ```bash
   git tag v0.0.1
   git push origin v0.0.1
   ```

   タグ名はバージョンに `v` を前置した形式(`v0.0.1` など)にする。
5. **Actionsの実行を確認**する。GitHubの Actions タブで `Publish` ワークフローを開き、以下の順で成功することを確認する。
   1. `build`(sdist/wheelのビルド)
   2. `testpypi`(TestPyPIへの公開)
   3. `pypi`(PyPIへの本番公開。`testpypi` の成功後にのみ実行される)
6. **インストール確認**をする。

   ```bash
   pip install duckdb-nb-export==0.0.1
   ```

   バージョンは手順1で上げた番号に置き換える。

## 3. 予行のみ行いたい場合

本番に公開せずTestPyPIだけで動作確認したい場合は、タグをpushせず、GitHubのActionsタブから `Publish` ワークフローを `workflow_dispatch` で手動実行する。

- `workflow_dispatch` 実行時は `build` → `testpypi` のみが動作し、`pypi` ジョブは実行されない(`if: github.event_name == 'push'` により、手動実行では条件を満たさずスキップされる)。
- TestPyPI上のバージョンを確認したい場合は次の通り。

  ```bash
  pip install -i https://test.pypi.org/simple/ duckdb-nb-export==0.0.1
  ```

## 4. 注意事項

- **バージョン番号は再利用できない**: PyPI/TestPyPIともに、一度公開したバージョン番号は(削除後であっても)同じ番号で再公開できない。誤ったリリースをした場合は必ず新しいバージョン番号を使う。
- **ワークフロー名・environment名を変更した場合は、PyPI側の Trusted Publisher 登録も同時に更新する**こと。`.github/workflows/publish.yml` のファイル名(`publish.yml`)や、`pypi` / `testpypi` という environment 名は、PyPI/TestPyPIの登録内容と一致している必要がある。片方だけ変更すると、OIDCでの認証に失敗し公開が失敗する。
- タグpushは `main` にマージ済みのコミットに対して行うこと。タグの指す内容がそのままリリース物になる。
