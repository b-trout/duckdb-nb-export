# テスト設計書: DuckDB UI Notebook → HTML Export ツール

- ステータス: テストスイート実装済み(106 passed / 5 skipped)
- 作成日: 2026-07-05
- 改訂: 2026-07-05(notebook JSONスキーマ実機調査(design doc 6.2#1/#3)完了を反映。2.1節・2.2節のブロック状況、8.2節のブロック理由、6.2節AT-010の内容を更新。新たな設計判断はなし)
- 改訂: 2026-07-05(整合性レビュー反映。実装済みテストスイートとの不整合を解消。新たな設計判断はなし)
- 改訂: 2026-07-05(第7回改訂 — `--notebook-id` オプション追加とui.db不在エラー改善に伴い、UT-R-014・UT-R-015・UT-C-023を追加。design doc 4.1節/7章の第7回改訂と対応)
- 対象範囲: **Phase 1(MVP: CLIエクスポート)のみ**。Phase 1.5(マジックコマンド)・Phase 2(チャート埋め込み・C++コア移植)のテストは本書のスコープ外とする(関連ドキュメントのフェーズ分けは design doc 2.1節を参照)。
- 関連ドキュメント:
  - Design Doc: `docs/design/duckdb-notebook-html-export-design.md`
  - ADR集: `docs/adr/duckdb-notebook-html-export-adr.md`
  - 実機検証スクリプト(前提検証テストの移植元): `scripts/verify_duckdb_assumptions.py`

## 0. 前提とする実装技術

| 項目 | 内容 |
|---|---|
| テストフレームワーク | pytest |
| パッケージ・実行管理 | uv(`uv sync` / `uv run pytest`) |
| Python バージョン | `>=3.11`(pyproject.toml と一致) |
| CI | GitHub Actions(`.github/workflows/ci.yml`)。Linux(ubuntu-latest)。Phase 1はLinux/macOSのみ正式対応のため、CIマトリクスもLinux/macOSのみで構成する(design doc 8章、ADR-008) |
| 対象コマンド | `duckdb-nb-export`(design doc 7章) |

### テストダブル方針

本ツールの価値の大部分は「DuckDBの実際のトランザクション・ロック・エラー挙動」に依存する(design doc 6.3節・8章「前提検証テスト」参照)。したがって以下の方針を徹底する。

| 対象 | 方針 | 理由 |
|---|---|---|
| DuckDB本体(Reader/Executorが接続する対象DB・ui.dbコピー) | **モックしない。常に実DuckDB(インメモリ or 一時ファイル)を使う** | 実DuckDBの挙動(SAVEPOINT非対応、abort伝播、ROLLBACK範囲、ロック挙動等)自体が設計前提そのものであり、モックすると検証対象が消える(design doc 6.3節) |
| TTY判定(`sys.stdin.isatty()` 等) | モック可 | 非TTY環境の分岐(design doc 7章、終了コード5)をCI等の実行環境に依存せず再現するため |
| 時刻・タイムアウト待機 | モック可(または短縮した実値を使用) | 300秒タイムアウト・30秒猶予・0.5秒リトライ待機をテスト実行時間内に収めるため。ただし実際の秒数を検証するテストケースは実時間 or `time.monotonic` のfake clockで行う |
| ファイルシステム境界(出力先パスの許可ベースディレクトリ判定、symlink解決) | 境界判定ロジックの単体テストでは一時ディレクトリ構造を組んで実ファイルシステムで検証する(モック不要な領域だが、OS依存差異を避けるため`tmp_path`フィクスチャで隔離する) | パストラバーサル対策(design doc 5章、ADR-006)の正しさはOSの実パス解決に依存するため |
| 外部プロセス(subprocess でのロック保持プロセス等) | モックしない。実際に別プロセスを起動する(統合テストのみ) | ロック競合(6.3#4)はプロセス境界がなければ再現できない |

上記以外(CLIの引数パース結果、Jinja2テンプレートへの入力データ構造等)は通常のユニットテストの対象としてモック不要。

---

## 1. テスト戦略概要

### 1.1 テストピラミッド

本ツールは「単体」「統合」「E2E」の一般的な3層に加えて、DuckDBという外部依存の挙動そのものを監視する第4のカテゴリ「前提検証テスト」を持つ。

| 層 | 目的 | 実DuckDB使用 | 対応する設計箇所 |
|---|---|---|---|
| ① 単体テスト | Reader/Executor/Renderer/CLIの各層を個別に検証する | 使用(ただし1関数・1クラス単位の狭い範囲) | design doc 4章各層、5章、7章 |
| ② 統合テスト | 複数層を結合し、実DuckDBファイル・実プロセスを介した相互作用を検証する | 使用(ファイル・プロセス境界含む) | design doc 4.1〜4.2節、ADR-002, ADR-007 |
| ③ E2Eテスト(ゴールデンテスト) | CLIをプロセスとして起動し、終了コードと出力HTMLのスナップショットを検証する | 使用 | design doc 7章、8章「ゴールデンHTMLテスト」 |
| ④ 前提検証テスト | design doc 6.3節の実機検証8項目およびDuckDB UIの非公式スキーマを恒久的なpytestとして監視し、`duckdb` パッケージ更新・DuckDB UI更新で設計前提が崩れていないかを検知する | 使用 | design doc 6.3節、6.2#1、8章、ADR-007 |

④は他の3層と性質が異なる特殊カテゴリである点を強調する。①〜③が「実装がdesign doc通りに動くか」を検証するのに対し、④は「design doc の前提としているDuckDB自体の挙動・DuckDB UIの非公式スキーマが今も成立しているか」を検証する。したがって④のテストが失敗した場合、それは実装のバグではなく、**design doc・ADRの再検討が必要というシグナル**である(詳細は6章の運用注記を参照)。

### 1.2 各層の位置づけと本書での表記

以降、各テストケースには `<種別>-<層>-<連番>` 形式のIDを付与する。種別・層の略号は以下のとおり。

| 略号 | 意味 |
|---|---|
| UT | 単体テスト(Unit Test) |
| IT | 統合テスト(Integration Test) |
| E2E | E2Eゴールデンテスト |
| AT | 前提検証テスト(Assumption Test) |
| R | Reader層 |
| X | Executor層(eXecutor。Eは既にE2Eで使用のため) |
| RD | Renderer層 |
| C | CLI層 |

例: `UT-R-001`(単体・Reader層・1番目)、`AT-001`(前提検証テスト、層をまたがないため層記号なし)。

---

## 2. フィクスチャ・テストデータ管理方針

### 2.1 ui.db フィクスチャ(実UI由来・バイナリ)

- 実際のDuckDB UI(`duckdb --ui`)で作成した `ui.db`(と、存在する場合は `ui.db.wal`)を `tests/fixtures/ui_db/` 配下にバイナリのまま保持する。現在のフィクスチャはwalなしである。
- 目的は非公式スキーマ(`notebooks` / `notebook_versions`)の変化をCIで機械的に検知することである(design doc 8章「ui.dbスキーマのフィクスチャテスト」)。検知は前提検証テスト AT-009(テーブル・カラム構造)および AT-010(notebook JSONのPydanticパース)としてID付きテストケース化する(6章)。
- notebook JSONスキーマ実機調査(design doc 6.2#1/#3)完了により、ui.dbの正確なDDL・notebook保存形式v3の構造が確定した(design doc 6.3#9)。これにより実UIフィクスチャの生成手順(下記スクリプト化)・内容検証(AT-009/AT-010)を、推測ではなく確定済みスキーマに基づいて実装できるようになった。
- **再生成手順のスクリプト化**: フィクスチャは手作業では再現性がないため、`scripts/regenerate_ui_db_fixtures.py`(実装済み)で再生成する。スクリプトはUIサーバーを起動し、ブラウザでの実UI操作を促した上で、ブラウザが使えない環境では確定済みスキーマ(design doc 6.3#9)に基づくフォールバック構築を行う。手動実行(CI上で `duckdb --ui` を自動操作するのは非現実的なため)を前提とし、DuckDBバージョンアップ時やスキーマ変化検知時に開発者が明示的に再生成する運用とする。
- **現在のフィクスチャの出自(2026-07-05)**: 初回生成はブラウザが利用できない環境だったため、**フォールバック(実UIフロントエンドと同一のDDL・JSON v3形式によるSQL直接構築)で生成**されている。実ブラウザ操作由来ではない点に留意(スキーマ・形式はバンドル抽出のDDL/バリデータに厳密準拠しており、AT-009/010の検知目的には十分)。ブラウザが使える環境で再生成した際はこの注記を更新すること。
- フィクスチャは最小限のケース(1notebook・複数バージョン・複数セル・同名notebook衝突用の2件)を用意する。

### 2.2 合成 ui.db フィクスチャ(生成ヘルパー)

- 実UI依存のバイナリフィクスチャだけでは、エラーセル・abortセル・大量行・NULL/ネスト型といった多様なテストケースを網羅するnotebookを都度実UIで作るのは非現実的である。
- そのため、notebook JSONスキーマの実機調査(design doc 6.2#1、Phase 1実装の前提タスク)完了後に、テストコードから `notebook_versions` 相当のテーブル・レコードを直接組み立てる生成ヘルパー(`tests/helpers/synthetic_ui_db.py` の `build_ui_db(notebooks, dest_dir) -> Path` のような関数)を用意する。
- このヘルパーは実DuckDB UIに依存せず、pytestのユニット/統合テストから任意のセル構成(SQL文字列・想定エラー種別等)を持つ `ui.db` を動的に生成できるようにする。
- **ブロック解除(2026-07-05)**: notebook JSONスキーマ実機調査(6.2#1)が完了し、ui.dbの正確なDDL・notebook保存形式v3の構造(`cells[].query`、`cellId`等)が確定したため、`build_ui_db` の実装に着手可能になった。ただし保存形式v3には `cell_type`/`cellType` に相当するキーが存在せず、チャートセルの保存表現自体がない(design doc 6.3#9)。そのため `build_ui_db` はSQLセル(`cell_type == "sql"` 相当)のみを生成対象とし、それ以外の `cell_type` を渡された場合は `NotImplementedError` を送出する仕様とする。この制約とチャートセルフィクスチャが引き続き生成不能である点の詳細は8.2節を参照。

### 2.3 ゴールデンHTML比較方針

- E2Eテストの出力HTMLには可変メタデータ(design doc 4.3節「出力メタデータ」: エクスポート実行日時UTC、DuckDBバージョン、notebookバージョンID、ツールバージョン)が含まれるため、単純な文字列一致比較はできない。
- 比較前に以下の正規化(プレースホルダ置換)を行う。

| 可変要素 | 正規化後のプレースホルダ | 対応する設計箇所 |
|---|---|---|
| エクスポート実行日時(ISO8601 UTC) | `__EXPORT_TIMESTAMP__` | design doc 4.3節 |
| DuckDBバージョン文字列 | `__DUCKDB_VERSION__` | design doc 4.3節 |
| notebookバージョンID | `__NB_VERSION_ID__` | design doc 4.3節 |
| ツール自体のバージョン | `__TOOL_VERSION__` | design doc 4.3節 |

- 正規化後の文字列を、`tests/golden/*.html` に保存済みの期待値と比較する。差分がある場合はテストが失敗し、意図した変更であれば golden ファイルを更新する。更新手段は `UPDATE_GOLDEN_HTML=1 uv run pytest tests/test_e2e.py` として実装済みである。

### 2.4 storage version不整合フィクスチャ

- storage version不整合時のエラーメッセージ検証(UT-R-013、design doc 6.2#10・8章)には、「ツールが依存する `duckdb` パッケージより新しいstorage versionで作成されたui.db」フィクスチャが必要である。より新しいDuckDBバージョンで事前生成したDBファイルを `tests/fixtures/storage_version/` 配下に保持する。
- このフィクスチャは時間経過とともに「新しくない」状態になる。テスト実行環境の `duckdb` パッケージが十分新しくなり、フィクスチャのstorage versionを問題なく開けるようになった場合は、当該テストを `pytest.skip` する(skip理由に「フィクスチャの再生成が必要」であることを明示する)運用とする。skipの常態化を検知した時点で、より新しいDuckDBでフィクスチャを再生成する。

---

## 3. 単体テスト

各層につき1関数・1クラス相当の狭い範囲を対象とする。実DuckDBは使うが、ファイル・プロセス境界をまたぐケース(ロック保持プロセスとの競合等)は4章の統合テストに配置する。

### 3.1 Reader層

| ID | テストケース | トレーサビリティ |
|---|---|---|
| UT-R-001 | `ui.db.wal` が存在する場合、コピー対象に `ui.db` と `ui.db.wal` の両方が含まれること | design doc 4.1節, ADR-002 |
| UT-R-002 | `ui.db.wal` が存在しない場合、`ui.db` 単体のコピーで正常に読み取れること | design doc 4.1節, ADR-002 |
| UT-R-003 | コピー後の読み取り検証が成功する場合、リトライなしで即座に成功すること | design doc 4.1節, ADR-002 |
| UT-R-004 | コピー直後の読み取り検証が失敗する場合、既定0.5秒間隔で既定3回までリトライされること | design doc 4.1節, ADR-002 |
| UT-R-005 | 3回のリトライを尽くしても読み取りに失敗する場合、「UIが起動中のため読み取りに失敗した。`--require-ui-closed` で再試行するか、少し時間をおいて再実行してください」という趣旨の明確なエラーメッセージを返すこと | design doc 4.1節, ADR-002 |
| UT-R-006 | 指定したnotebook名が存在しない場合、存在するnotebook名一覧を添えたエラーを返すこと | design doc 4.1節 |
| UT-R-007 | 同名notebookが複数存在する場合、曖昧さを解決せず、候補(名前・内部ID・更新日時)を一覧表示してエラーとすること | design doc 4.1節 |
| UT-R-008 | `--nb-version` 未指定時は既定で最新バージョンが選択されること | design doc 4.1節 |
| UT-R-009 | `--nb-version <id>` 指定時は指定したバージョンIDのnotebook定義が取得されること | design doc 4.1節 |
| UT-R-010 | notebook JSONに未知のフィールドが含まれていても、Pydanticモデルのパースが失敗しないこと(将来のスキーマ変更耐性) | design doc 4.1節 |
| UT-R-011 | `--ui-db <path>` を指定した場合、既定パス(`<HOME>/.duckdb/extension_data/ui/ui.db`)ではなく指定パスが読み込まれること | design doc 4.1節, 7章 |
| UT-R-012 | `--require-ui-closed` 指定時、UI非稼働(ロックなし)であればスナップショットコピーを介さずui.dbを直接開いて読み取りが成功すること | design doc 4.1節, 7章, ADR-002 |
| UT-R-013 | ツールの `duckdb` パッケージより新しいstorage versionで作成されたui.dbを開こうとした場合、「ツール側のduckdbパッケージ更新」を促す趣旨の明確なエラーメッセージ(英語)が返ること(CLIとしては終了コード4 — UT-C-018と相互参照。フィクスチャは2.4節。実行環境のduckdbが十分新しい場合はskip) | design doc 6.2#10, 8章, 7章, ADR-001 |
| UT-R-014 | `notebook_id` を指定した場合、同名notebookが複数存在していても曖昧エラーにならず、指定IDのnotebookが一意に解決されること(同名衝突の脱出路) | design doc 4.1節, 7章(第7回改訂) |
| UT-R-015 | `ui.db` ファイル自体が存在しない場合、「見つからない」旨の明確なエラーになり、「UIが起動中かもしれない」という誤誘導メッセージにならないこと | design doc 4.1節, 7章(第7回改訂) |

### 3.2 Executor層

| ID | テストケース | トレーサビリティ |
|---|---|---|
| UT-X-001 | エクスポート実行が新規接続で `BEGIN TRANSACTION` を発行してから全セルを実行すること | design doc 4.2節, ADR-007 |
| UT-X-002 | 既定(`--allow-writes` 未指定)では全セル実行後に `ROLLBACK` が発行され、対象DBへの変更が反映されないこと | design doc 4.2節, ADR-007 |
| UT-X-003 | `--allow-writes` 指定時は全セル実行後に `ROLLBACK` の代わりに `COMMIT` が発行され、対象DBへの変更が反映されること | design doc 4.2節, ADR-007 |
| UT-X-004 | 非abort系エラー(例: `CatalogException` = テーブル不在)が発生したセルの後続セルが、継続して実行されること | design doc 4.2節, 6.3#3, ADR-007 |
| UT-X-005 | abort系エラー(例: `ConstraintException`)が発生した場合、以降の後続セルは実行されず「トランザクション中断のためスキップ」としてマークされること | design doc 4.2節, 6.3#3, ADR-007 |
| UT-X-006 | abort判定はエラー捕捉直後に `SELECT 1` のプローブ文を実行し、`TransactionException` が返る場合にabort状態と判定すること | design doc 4.2節, ADR-007 |
| UT-X-007 | セルSQLに `BEGIN` が含まれる場合、SQLパース(`duckdb.extract_statements` 相当)による判定で実行せず失敗扱いとすること | design doc 4.2節, 6.3#3, ADR-007 |
| UT-X-008 | セルSQLに `COMMIT` / `ROLLBACK` が含まれる場合も同様に実行せず失敗扱いとすること | design doc 4.2節, ADR-007 |
| UT-X-009 | セルSQL中の文字列リテラル内に `'BEGIN'` という文字列が含まれる場合、トランザクション制御文として誤検出しないこと | design doc 4.2節(SQLパース判定) |
| UT-X-010 | セルSQL中のコメント内に `-- BEGIN` が含まれる場合、トランザクション制御文として誤検出しないこと | design doc 4.2節(SQLパース判定) |
| UT-X-011 | セル実行が既定300秒(`--cell-timeout` で変更可)を超過した場合、`connection.interrupt()` が呼ばれること | design doc 4.2節, 6.3#7, ADR-007 |
| UT-X-012 | interruptが猶予(既定30秒)以内に効いた場合、そのセルは失敗として扱われ、後続セルの処理が継続されること | design doc 4.2節, ADR-007 |
| UT-X-013 | interruptが既定30秒の猶予内に効かない場合、後続セルの実行を断念し、取得済み結果で部分HTMLを生成した上で終了コード2で終了すること | design doc 4.2節(第4回改訂), ADR-007 |
| UT-X-014 | 対象DB解決: `--db` が指定されている場合、notebook JSON内の情報より `--db` が優先されること | design doc 4.2節, ADR-008 |
| UT-X-015 | 対象DB解決: `--db` 未指定かつnotebook JSON内に接続情報がある場合、その情報から対象DBが解決されること | design doc 4.2節, ADR-008 |
| UT-X-016 | 対象DB解決: `--db` 未指定かつnotebook JSONからも解決できない場合、`:memory:` にフォールバックし、標準エラー出力とHTML内メタデータに警告が明示されること | design doc 4.2節, ADR-008 |
| UT-X-017 | `--max-rows` 既定値1,000に対し、結果セットの取得が上限+1行(1,001行)のみであること(fetchmany相当。全件fetchしないこと) | design doc 4.3節 |
| UT-X-018 | 1セルに複数ステートメントが含まれる場合、すべて順に実行されるが、表示されるのは最後のステートメントの結果セットのみであること | design doc 4.3節(第3回改訂) |
| UT-X-019 | `--stop-on-error` 指定時、エラー種別を問わず最初の失敗で処理が中断されること | design doc 4.2節 |
| UT-X-020 | `--no-external-access` 指定時、`SET enable_external_access = false` に相当する設定で実行されること | design doc 4.2節, 5章, ADR-006 |
| UT-X-021 | notebook JSONのDB名(`currentDatabase`/`useDatabase`)がベストエフォートの `USE` で再現されること — notebookレベルはBEGIN直後、セルレベルは当該セル直前に適用され、ATTACH済みカタログ名なら切り替わること | design doc 4.2節, ADR-008 |
| UT-X-022 | 解決不能なDB名の `USE` 失敗時、警告(同一名につき1回)を出して現在のデータベースのままセル実行が継続されること | design doc 4.2節, ADR-008 |
| UT-X-023 | タイムアウトによるトランザクションabort復旧(ROLLBACK+BEGIN)後、トランザクション内ATTACH由来のカタログが失われてデフォルトデータベースが無効になっている場合、プライマリカタログへ警告付きで復元されること | design doc 4.2節, ADR-007, ADR-008 |
| UT-X-024 | notebookレベルの `currentDatabase` が(未ATTACHのため)一度失敗して警告が出た後、後続セルのATTACHにより同名カタログが解決可能になった場合、セルレベルの `useDatabase` の `USE` 試行自体は抑止されず再試行され成功すること(警告は失敗時のみ・同一名につき1回のまま) | design doc 4.2節, ADR-008 |

### 3.3 Renderer層

| ID | テストケース | トレーサビリティ |
|---|---|---|
| UT-RD-001 | セル値に `<script>` 等のHTMLタグ文字列が含まれる場合、autoescapeによりエスケープされてHTMLに埋め込まれること(XSS防止) | design doc 5章, ADR-006 |
| UT-RD-002 | セルSQLの文字列自体に `<`、`>`、`&` 等が含まれる場合もエスケープされて表示されること(注: Pygmentsハイライトのトークン分割により、エスケープ検証は単一トークン内で完結するアサーションで行う — 実装時裁定) | design doc 5章, ADR-006 |
| UT-RD-003 | `CREATE SECRET` 文のすべてのパラメータ値が `***` にマスクされること | design doc 4.3節, 5章, ADR-006 |
| UT-RD-004 | `CREATE SECRET` 文のうち TYPE・PROVIDER・SCOPE 等の構造的要素はマスクされず残ること | design doc 5章, ADR-006 |
| UT-RD-005 | NULL値は空文字列ではなく `NULL` と明示表示されること | design doc 4.3節 |
| UT-RD-006 | STRUCT型の値がDuckDBの文字列表現で表示されること | design doc 4.3節 |
| UT-RD-007 | LIST型の値がDuckDBの文字列表現で表示されること | design doc 4.3節 |
| UT-RD-008 | MAP型の値がDuckDBの文字列表現で表示されること | design doc 4.3節 |
| UT-RD-009 | BLOB型の値は値そのものではなくサイズ表示になること | design doc 4.3節 |
| UT-RD-010 | 既定500文字を超える文字列値は500文字で切り詰められ、全長が注記されること | design doc 4.3節(第4回改訂) |
| UT-RD-011 | 500文字以内の文字列値は切り詰められず全体が表示されること | design doc 4.3節 |
| UT-RD-012 | 表示行数が既定1,000行の上限を超える場合、「先頭1,000行を表示(1,000行超。総件数は未計測)」という趣旨の表記になること | design doc 4.3節(第3回改訂) |
| UT-RD-013 | 結果セットを返さない文(DDL等)は「OK」等の完了表示になること | design doc 4.3節 |
| UT-RD-014 | DML文は影響行数が表示されること | design doc 4.3節 |
| UT-RD-015 | チャートセルはSQL実行結果を通常のテーブルとして代替表示し、「チャート表示はPhase 1では非対応(テーブルで代替表示)」という趣旨の英語注記が付くこと | design doc 4.3節, ADR-004 |
| UT-RD-016 | abort系エラーによりスキップされたセルは、「トランザクション中断のためスキップ」という趣旨の表示がHTML上に明示されること | design doc 4.2節, ADR-007 |
| UT-RD-017 | HTML内のメタデータに「エクスポート実行日時(UTC, ISO8601)」が含まれること | design doc 4.3節 |
| UT-RD-018 | HTML内のメタデータに「DuckDBのバージョン」が含まれること | design doc 4.3節 |
| UT-RD-019 | HTML内のメタデータに「対象notebookのバージョン識別子」が含まれること | design doc 4.3節 |
| UT-RD-020 | HTML内のメタデータに「本ツール自体のバージョン」が含まれること | design doc 4.3節 |
| UT-RD-021 | 生成された単一HTMLファイル内に、外部リソースを読み込む参照(`<link href>`・`<script src>`・`<img src>`・CSSの `@import`・`url()` 等)が一切存在しないこと(機械的な検査。CSS・JSがすべてインライン埋め込みであることの確認)。セルの結果データ値としてのURL文字列(例: URL列を持つテーブル)はエスケープ済みテキストであり検査対象外として許容する | design doc 4.3節 |
| UT-RD-022 | HTML内の注記文言・エラーメッセージ文言が英語であること | design doc 4.3節(第4回改訂) |
| UT-RD-023 | 生成CSSに `prefers-color-scheme` メディアクエリが含まれ、ダークモード自動対応のスタイルが定義されていること | design doc 4.3節(第4回改訂) |

### 3.4 CLI層

| ID | テストケース | トレーサビリティ |
|---|---|---|
| UT-C-001 | `-o`/`--output` 未指定時、出力パスが絶対パスへ正規化され、既定の許可ベースディレクトリ(カレントディレクトリ)配下であれば許可されること | design doc 5章, 7章, ADR-006 |
| UT-C-002 | 出力パスが正規化(symlink解決含む)後に許可ベースディレクトリ外を指す場合、終了コード3で拒否されること | design doc 5章, 7章, ADR-006 |
| UT-C-003 | 出力パスが `..` を含む文字列であっても、正規化後のパス比較で判定され、単純な文字列拒否ではないこと(許可ベース配下に収まる `..` は許可される一方、脱出するものは拒否されること) | design doc 5章, ADR-006 |
| UT-C-004 | 出力パスがベースディレクトリ配下のsymlinkを経由して実際にはベースディレクトリ外を指す場合、正規化後の実パスで判定され拒否されること(終了コード3) | design doc 5章, ADR-006 |
| UT-C-005 | 指定した出力先パスに既にファイルが存在する場合、`<name>-1.html` のような連番付与で衝突を回避すること | design doc 7章 |
| UT-C-006 | 連番付与後のファイルも存在する場合、`-2`, `-3` と番号がインクリメントされること | design doc 7章 |
| UT-C-007 | notebook名にファイル名として不正な文字・空白が含まれる場合、`_` に置換されること | design doc 7章 |
| UT-C-008 | notebook名の不正文字が置換された場合、置換後の名前が警告として表示されること | design doc 7章 |
| UT-C-009 | `--output-dir <dir>` 指定時、許可ベースディレクトリがカレントディレクトリから指定ディレクトリに切り替わり、指定ディレクトリ配下への出力は許可され、その外を指す出力パスは終了コード3で拒否されること | design doc 5章, 7章, ADR-006 |
| UT-C-010 | 非TTY環境(stdinがTTYでない)かつ確認プロンプトが必要な場面で `--yes` が指定されていない場合、プロンプトを出さずエラー終了(終了コード5)すること | design doc 5章, 7章 |
| UT-C-011 | `--yes` 指定時は確認プロンプトがスキップされ処理が継続すること | design doc 7章 |
| UT-C-012 | 確認プロンプトが表示される場合、対象notebookの全セルのSQL本文一覧がコンソールに表示されること | design doc 5章, ADR-006 |
| UT-C-013 | 正常終了(セル失敗を含んでいても `--stop-on-error` 未指定なら完走)した場合、終了コード0であること | design doc 7章 |
| UT-C-014 | notebookが見つからない場合(同名複数一致による曖昧エラー含む)、終了コード1であること | design doc 7章 |
| UT-C-015 | `--stop-on-error` 指定時にセル実行エラーで中断した場合、終了コード2であること | design doc 7章 |
| UT-C-016 | タイムアウト後のinterruptが猶予内に効かず全体中断した場合、終了コード2かつ部分HTMLが生成されること | design doc 7章, 4.2節 |
| UT-C-017 | 出力パスが許可ベースディレクトリ外でセキュリティ検査により拒否された場合、終了コード3であること | design doc 7章 |
| UT-C-018 | ui.dbへのアクセス失敗(ロック・破損・storage version不整合等)の場合、終了コード4であること(storage version不整合時のエラーメッセージ内容の検証はUT-R-013と相互参照) | design doc 7章 |
| UT-C-019 | 確認プロンプトでユーザーが実行を拒否した場合、終了コード5であること | design doc 7章 |
| UT-C-020 | `--list` 指定時、notebook一覧(名前・ID・更新日時)が表示されて終了すること(位置引数省略可) | design doc 7章 |
| UT-C-021 | `--list-versions` 指定時、指定notebookのバージョン一覧(ID・作成日時)が表示されて終了すること | design doc 7章(第4回改訂 #19) |
| UT-C-022 | `-o` 未指定時、既定の出力パスが `./<notebook-name>.html`(notebook名に不正文字がある場合はサニタイズ後の名前)になること | design doc 7章 |
| UT-C-023 | `--notebook-id <id>` 指定時、同名notebookが複数存在していてもIDで一意に解決してエクスポートでき、位置引数 `<notebook-name>` を省略できること | design doc 4.1節, 7章(第7回改訂) |

---

## 4. 統合テスト

実DuckDBファイル・複数プロセスを用いて層を結合した挙動を検証する。プロセス境界を必要とするケースは実際にsubprocessを起動する(モックしない)。

| ID | テストケース | トレーサビリティ |
|---|---|---|
| IT-001 | 別プロセスがui.dbに対してRW接続を保持している状態でも、Reader層のコピー方式による読み取りが成功すること(subprocessでRW接続を保持させ、その間に本体プロセスからコピー読み取りを実行) | design doc 4.1節, 6.3#4, 6.3#5, ADR-002 |
| IT-002 | 別プロセスがui.dbのRW接続を保持している状態で、コピーを介さず直接読み取ろうとすると失敗すること(コピー方式の必要性を裏付ける対照実験) | design doc 4.1節, 6.3#4, ADR-002 |
| IT-003 | Executor実行結果(エラーセルを含む)がRenderer層で正しくHTMLへ反映されること(非abort系エラーセルがエラー表示、後続セルは通常表示) | design doc 4.2節, 4.3節, ADR-007 |
| IT-004 | Executor実行結果(abort系エラーを含む)がRenderer層で正しくHTMLへ反映されること(abort後の後続セルがすべて「スキップ」表示になること) | design doc 4.2節, 4.3節, ADR-007 |
| IT-005 | セルタイムアウト(interruptが効いて中断)がRenderer層で正しくHTMLへ反映されること(タイムアウトしたセルが失敗表示になること) | design doc 4.2節, 4.3節 |
| IT-006 | `--allow-writes` を指定して実行した場合、対象DBファイルへの変更が実際にコミットされ、エクスポート終了後も対象DBファイルに反映が残っていること | design doc 4.2節, ADR-007 |
| IT-007 | `--allow-writes` を指定しない場合、対象DBファイルへの変更がエクスポート終了後に残っていない(ROLLBACK済み)こと | design doc 4.2節, ADR-007 |
| IT-008 | `COPY ... TO` を含むセルを実行し `--allow-writes` を指定しない(ROLLBACK経路)場合でも、COPY TOによって書き出された外部ファイルはロールバック後も残存すること | design doc 4.2節, 5章, 6.3#6, ADR-006 |
| IT-009 | IT-008のようにファイル書き出しを伴うnotebookをCLIで実行する際、`--yes` 未指定であれば実行前確認プロンプトが表示される経路をたどること(外部副作用の防波堤としての確認プロンプト) | design doc 5章, ADR-006 |
| IT-010 | 別プロセスがui.dbのRW接続を保持している状態で `--require-ui-closed` を指定して実行した場合、ui.dbへのアクセス失敗として終了コード4でエラーになること(subprocessでロックを保持させて検証) | design doc 4.1節, 7章, 6.3#4, ADR-002 |

---

## 5. E2E(ゴールデンHTML)テスト

CLI(`duckdb-nb-export`)をsubprocessとして実際に起動し、終了コードと2.3節の正規化を経たHTMLスナップショットの両方を検証する。フィクスチャは2.2節の合成 ui.db 生成ヘルパー(スキーマ調査完了後)または2.1節の実UI由来フィクスチャを用いる。

| ID | notebookシナリオ | 検証内容 | トレーサビリティ |
|---|---|---|---|
| E2E-001 | 正常セルのみで構成されたnotebook | 終了コード0、全セルの結果テーブルが正しく描画されたゴールデンHTMLと一致すること | design doc 8章, 4.3節 |
| E2E-002 | 非abort系エラーセルを含む(後続は継続) | 終了コード0、エラーセルの表示とその後続セルの正常表示がゴールデンHTMLと一致すること | design doc 4.2節, 8章, ADR-007 |
| E2E-003 | abort系エラーが発生するnotebook(以降スキップ) | 終了コード0(既定は継続扱い)、abort以降のセルがすべて「スキップ」表示になっているゴールデンHTMLと一致すること | design doc 4.2節, 8章, ADR-007 |
| E2E-004 | 表示上限(1,000行)を超える大量行を返すセルを含む | 「1,000行超・総件数未計測」の表記を含むゴールデンHTMLと一致すること | design doc 4.3節, 8章 |
| E2E-005 | NULL値・STRUCT/LIST/MAPのネスト型を含むnotebook | NULL表示・ネスト型の文字列表現がゴールデンHTMLと一致すること | design doc 4.3節, 8章 |
| E2E-006 | チャートセルを含むnotebook | チャートセルがテーブル代替表示+非対応注記になっているゴールデンHTMLと一致すること | design doc 4.3節, ADR-004, 8章 |
| E2E-007 | `CREATE SECRET` 文を含むnotebook | パラメータ値がマスクされ、TYPE/PROVIDER/SCOPE等の構造的要素のみ残ったゴールデンHTMLと一致すること | design doc 5章, ADR-006, 8章 |
| E2E-008 | セル内に `BEGIN`/`COMMIT`/`ROLLBACK` を含むnotebook | 該当セルが実行されず失敗扱いとして表示されるゴールデンHTMLと一致すること | design doc 4.2節, ADR-007, 8章 |

各ケースは共通して以下も検証する。

- CLIプロセスの終了コードが期待値と一致すること。
- 生成物が単一HTMLファイルであり、外部リソースを読み込む参照(`<link href>`・`<script src>`・`<img src>`・`@import`・`url()` 等)を含まないこと(UT-RD-021のE2E版。データ値としてのURL文字列は許容)。

---

## 6. 前提検証テスト

design doc 8章の方針に基づき、設計前提を監視する恒久テストをマーカー `@pytest.mark.assumptions` 付きで保守し、通常のテストと区別する(7章参照)。監視対象は以下の2種である。

1. **DuckDB自体の挙動**(AT-001〜AT-008, AT-011): design doc 6.3節「実機検証済み事項」の項目を `scripts/verify_duckdb_assumptions.py` の内容から移植する(design doc 8章、9章)。`duckdb` パッケージ更新による前提変化を検知する。
2. **DuckDB UIの非公式スキーマ**(AT-009〜AT-010): 実UI由来のui.dbフィクスチャ(2.1節)に対する読み取り検証により、非公式スキーマ・notebook JSONスキーマの変化を検知する(design doc 8章「ui.dbスキーマのフィクスチャテスト」、6.2#1)。

### 6.1 DuckDB自体の挙動の監視(AT-001〜AT-008, AT-011)

| ID | 検証項目(design doc 6.3節の番号) | 期待する現状の挙動 | トレーサビリティ |
|---|---|---|---|
| AT-001 | 6.3#1: SAVEPOINTのサポート | `SAVEPOINT` 発行が `ParserException` で失敗し、非対応であること | design doc 6.3節, ADR-007 |
| AT-002 | 6.3#2: DDL(CREATE/DROP TABLE)のROLLBACK巻き戻り | トランザクション内のCREATE/DROP TABLEがROLLBACK後に完全に巻き戻ること | design doc 6.3節, ADR-007 |
| AT-003 | 6.3#3: トランザクション内エラー後の継続可否 | CatalogException後は後続文が継続実行できること、ConstraintException後はトランザクション全体がabortし後続文が`TransactionException`等で失敗すること | design doc 6.3節, ADR-007 |
| AT-004 | 6.3#4: RW接続保持中のDBファイルへの別プロセスアクセス | 別プロセスからは `read_only` 指定でも一切開けず、`Conflicting lock is held` 相当のエラーになること | design doc 6.3節, ADR-002, ADR-005 |
| AT-005 | 6.3#5: ロック保持中のOSファイルコピー | Linux上でロック保持中でもOSファイルコピー自体は可能であること、かつ本体のみコピーではWALのみに存在する直近変更が読み取れないこと | design doc 6.3節, ADR-002 |
| AT-006 | 6.3#6: トランザクション内の `COPY TO` とROLLBACK | `COPY ... TO` で書き出したファイルがROLLBACK後も残存すること | design doc 6.3節, ADR-006, ADR-007 |
| AT-007 | 6.3#7: `connection.interrupt()` によるクエリ中断 | interruptによって実行中クエリが`InterruptException`相当で中断され、中断後も同一接続が再利用可能であること | design doc 6.3節, ADR-007 |
| AT-008 | 6.3#8: トランザクション内の ATTACH / CHECKPOINT / SET / BEGIN | ATTACH・CHECKPOINT・SETはトランザクション内で実行できること、トランザクション内での`BEGIN`はエラーになること | design doc 6.3節, ADR-007 |
| AT-011 | 6.3#10: 明示的トランザクション内でのinterruptによるトランザクションabort | 明示的トランザクション内で実行中のクエリをinterruptすると`InterruptException`で中断され、トランザクションがabort状態になること(プローブ文が`TransactionException`で失敗)、`ROLLBACK`後は同一接続が再利用可能なこと | design doc 6.2#9, 6.3#10, 4.2節, ADR-007 |

### 6.2 DuckDB UIの非公式スキーマの監視(AT-009〜AT-010)

| ID | 検証項目 | 期待する現状の挙動 | トレーサビリティ |
|---|---|---|---|
| AT-009 | ui.dbの非公式スキーマ構造 | 実UI由来のui.dbフィクスチャ(2.1節)に対し、`notebooks` / `notebook_versions` テーブルと `json` カラムの存在を前提とするReader層のクエリが成功すること(失敗=DuckDB UIの非公式スキーマが変化したシグナル) | design doc 8章「ui.dbスキーマのフィクスチャテスト」, 6.2#1, ADR-002 |
| AT-010 | notebook JSONのスキーマ | 実UI由来フィクスチャのnotebook JSONが**保存形式v3**(design doc 6.3#9)用のStoredNotebookモデル(Pydantic)でパースできること(失敗=notebook JSONスキーマが変化したシグナル) | design doc 8章, 6.2#1, 4.1節, 6.3#9 |

### 6.3 調査完了後に追加予定の前提検証テスト

予約枠はすべて消化済みである。AT-011(6.2#9: interrupt時のトランザクション状態遷移)は2026-07-05の実機調査完了に伴い期待挙動を確定し、6.1節の表へ移動した(明示的トランザクション内のinterruptはトランザクションをabort状態にする — design doc 6.3#10)。

### 運用注記

前提検証テスト(AT-001〜AT-011)が失敗した場合、それは**実装のバグを意味しない**。これらは「design doc・ADRが前提としている外部依存(DuckDB自体の挙動、DuckDB UIの非公式スキーマ)」を検証しているため、失敗は以下のいずれかを意味する。

- `duckdb` パッケージの新バージョンでdesign doc 6.3節の前提が変化した(例: 将来SAVEPOINTがサポートされる、abort挙動が変わる等)— AT-001〜AT-008, AT-011。
- OS・実行環境の違いにより6.3節の前提が成立しない(特にAT-004・AT-005はロック機構のOS依存性が高い)— AT-001〜AT-008, AT-011。
- DuckDB UI側の更新により非公式スキーマ・notebook JSONスキーマが変化した — AT-009〜AT-010。

いずれの場合も、**対応すべきはテストコードの修正ではなくdesign doc・ADRの再検討**である(該当するADR-002/005/006/007の見直し、必要ならdesign doc改訂)。AT-009/AT-010の失敗時はこれに加えて、Reader層実装の追従修正とui.dbフィクスチャの再生成(2.1節の再生成スクリプト)も伴う。CI上でこのマーカーのテストが失敗した場合は、通常のテスト失敗とは切り分けて扱う(7章参照)。

---

## 7. CI構成

### 7.1 既存CIへの組み込み

既存の `.github/workflows/ci.yml` は `pull_request`(対象: `main`)トリガーで、`uv sync` → `pre-commit run --all-files`(ruff-format/ruff-check/ty等) → `uv run pytest` という構成であり、Python `3.11` と `3`(最新3.x)のマトリクスで実行される。本ツールのテストスイートもこの枠組みに乗せる。

- 既存の `python-version: ["3.11", "3"]` マトリクスはそのまま維持する。
- Phase 1はLinux/macOSのみ正式対応(design doc 8章、ADR-008)のため、CIマトリクスに `os: [ubuntu-latest, macos-latest]` を追加する(現状は `ubuntu-latest` 固定)。Windows(`windows-latest`)は追加しない。
- `uv run pytest` のステップは現状維持(終了コード5「テスト未収集」を握りつぶす既存のハンドリングもそのまま利用可能)。

### 7.2 duckdbバージョンマトリクス

- pyproject.toml の依存は `duckdb>=1.5.4` であり上限を固定しない方針(design doc 8章)。CI側で以下のマトリクス軸を追加する。

| 軸 | 値 | 目的 |
|---|---|---|
| duckdbバージョン(最低サポート) | `1.5.4`(pyproject.toml記載の下限) | 最低サポートバージョンでの前提検証・回帰確認 |
| duckdbバージョン(最新) | PyPI最新版(`uv add duckdb --upgrade` 相当、またはバージョン指定なしの通常インストール) | 前提検証テスト(6章)が新バージョンで崩れていないかの継続監視 |

- 実装方法としては、`uv run --with duckdb==1.5.4 pytest` のようにduckdbバージョンだけを差し替えて**全テストスイート**を実行するジョブを、通常ジョブ(pyproject.toml通りの依存解決=実質最新)と並べてマトリクス化することを想定する(design doc 8章「サポートする複数の duckdb パッケージバージョンでテストを実行する」と整合)。

### 7.2.1 duckdb以外の依存フロア検証(2026-07-05追加)

- duckdb以外の直接依存(`jinja2` / `pydantic` / `pygments` / `structlog`)についても、pyproject.tomlの宣言フロアが実際にテストスイートを通ることをCIで検証する。専用ジョブ(`floors`)が `uv sync --resolution lowest-direct` で全直接依存を宣言下限に固定して全テストスイートを実行する。
- マトリクスには載せない(Ubuntu + Python 3.11 の1レグのみ)。フロア破損はOS依存の問題ではなく依存解決の問題であるため、1レグで十分とする。
- このジョブの失敗は**ブロッキング**である。フロアでスイートが通らない場合の対応はpyproject.tomlのフロアのバンプであり、テストの緩和ではない。
- 導入時の検証(2026-07-05)で `pygments>=2` のフロアが実際には壊れていることが判明した(Pygments 2.19.0でCSS色表記が `#F00` → `#FF0000` に変更され、golden HTMLが2.19未満と一致しない)ため、フロアを `pygments>=2.19` にバンプした。jinja2 3.0.0 / pydantic 2.0 / structlog 24.1.0 のフロアは全テスト通過を確認済み。

### 7.3 前提検証テストのマーカー分離

- `pytest.ini` または `pyproject.toml` の `[tool.pytest.ini_options]` に `markers = ["assumptions: 設計前提(DuckDB自体の挙動・DuckDB UIの非公式スキーマ)を検証する恒久テスト"]` を定義し、`@pytest.mark.assumptions` を付与する(監視対象2種の内訳は6章)。なお `pyproject.toml` には `assumptions` に加え `integration`・`e2e` の計3種のマーカーが定義済みである。
- 通常のCIジョブでは全テスト(単体・統合・E2E・前提検証)を実行するが、前提検証テストのみ以下のように扱いを分ける。
  - **duckdb最新バージョンのジョブでAT-*が失敗した場合**: CI全体を失敗させるのではなく、6章の運用注記のとおり「design doc再検討が必要」というシグナルとして扱う。**採用済み**の実装はジョブ単位ではなく**ステップ単位**の `continue-on-error` である。具体的には、`test` ジョブ自体には `continue-on-error` を設定せず、ジョブ内の「前提検証テスト実行」ステップ(`assumptions` マーカー付きテストのみを実行)にのみ `continue-on-error: ${{ matrix.duckdb-version != '1.5.4' }}` を条件付きで設定する。これにより、同一ジョブ内の通常テスト(`-m "not assumptions"`、最低サポートバージョン・最新バージョンいずれの行でも)の失敗はジョブ全体を失敗させたまま維持しつつ、前提検証テストの失敗のみ最新duckdbの行に限って非ブロッキングにする。専用のSlack通知・Issue起票等の後続アクションへの接続、mainブランチのマージ自体をブロックしない運用の最終確定は未決定事項として7.4に残す。
  - **最低サポートバージョンのジョブでAT-*が失敗した場合**: 通常の回帰として扱い、CIを失敗させる(サポート範囲内のバージョンでの前提崩れは看過しない)。

### 7.4 未決定事項

- 最新duckdbバージョンでの前提検証テスト失敗時に、CIを失敗させずに開発者へ通知する具体的な仕組み(GitHub Issue自動起票、Slack通知等)は本書執筆時点で未確定。

---

## 8. スコープ外・未決定事項

### 8.1 明示的にスコープ外とするテスト

| 項目 | 理由 | 参照 |
|---|---|---|
| Phase 1.5(マジックコマンド、`export_notebook_html()` スカラー関数)関連のテスト全般 | design doc 2.1節によりPhase 1のスコープ外。着手条件(対象DBロック問題の解決策確立、design doc 6.2#6)が満たされていない | design doc 2.1節, ADR-005 |
| Phase 2(チャートのクライアントサイド埋め込み、C++コアへの移植)関連のテスト全般 | design doc 2.1節によりPhase 1のスコープ外 | design doc 2.1節, ADR-004, ADR-005 |
| Windows環境でのテスト(ロック中ui.dbコピー可否含む) | Phase 1はLinux/macOSのみ正式対応(design doc 8章, ADR-008)。Windows検証はPhase 1.5以降のタスク(design doc 6.2#8) | design doc 8章, ADR-008 |
| チャートの実描画(Vega-Lite等)に関するテスト | Phase 1はチャート非対応であり、テーブル代替表示のみが対象(3.3節UT-RD-015、5章E2E-006で代替表示側は検証済み) | design doc 4.3節, ADR-004 |
| `'current'`(マジックコマンドから呼び出し元notebookを解決する仕組み)に関するテスト | Phase 1.5のGoal 2に属する機能であり、Phase 1のCLIには存在しない | design doc 2.2節 |
| 同一プロセス内でのui.db再アクセス(Phase 2、design doc 6.2#5)に関するテスト | Phase 2のアーキテクチャ(C++コア・同一プロセス内呼び出し)に依存 | design doc 6.2#5 |

### 8.2 チャートセルフィクスチャがブロックされるテスト群(2026-07-05更新: 6.2#1完了によりブロック解除の範囲を縮小)

design doc 6.2#1(「notebook JSONの正確なスキーマ」の実機調査)は完了し、ui.dbの正確なDDL・notebook保存形式v3の構造が確定した(design doc 6.3#9)。これにより、以下は**ブロック解除**された。

- **2.2節の合成ui.dbフィクスチャ生成ヘルパー(`build_ui_db`)**: 確定したJSONスキーマ(保存形式v3: `cells[].query`、`cellId`等)に基づき実装可能になった。
- **UT-X-014〜016(対象DB解決の優先順位)**: JSON内の `currentDatabase`/`cells[].useDatabase` がDB名のみでファイルパス・DSNを含まないことが判明した(6.2#3解決、design doc 6.3#9)ため、「JSON内の情報から解決」経路は事実上ほぼ発火せず `:memory:` フォールバックが主経路になる、という前提でテストデータを組める。
- **実行環境再現テスト(UT-X-021) — 解除(2026-07-05)**: 適用セマンティクスが確定し(ベストエフォート `USE`、失敗時は警告+続行 — design doc 4.2節、ADR-008追記)、UT-X-021(成功経路)・UT-X-022(失敗警告経路)として実装済み。ATTACH・拡張ロード・シークレット・変数の再現は保存形式v3に情報が存在しないため恒久的に対象外である。

一方、以下は**引き続きブロックされる**。理由は6.2#1完了によるものではなく、調査により**保存形式v3にチャート表現が存在しないことが判明した**ためである(design doc 6.2#2、6.3#9、ADR-004)。

- **チャートセル(E2E-006のフィクスチャ経由検証)**: 保存形式v3にはチャートセルの保存表現自体がないため、`build_ui_db` でチャートセルを含む `ui.db` フィクスチャを生成することはできない。`build_ui_db` はSQLセル(`cell_type == "sql"` 相当)以外を渡された場合に `NotImplementedError` を送出する仕様とし、E2E-006のうち `build_ui_db` 経由でのフィクスチャ生成を前提とする検証は、既存のskipパターン(2.4節のstorage version不整合フィクスチャと同様、`pytest.skip` に理由を明示)で明示的にスキップする。将来、別の保存場所でチャート設定が発見されるか、保存形式にチャート表現が追加された場合に解除する。なお UT-RD-015 はブロック対象ではない。`build_ui_db`/フィクスチャに依存せず、Notebook/Cellモデルを直接構築してレンダラーを検証する実装になっており、通常どおり実行・合格する(非skip)。

### 8.3 その他の未決定事項

- 7.3節で述べたとおり、前提検証テスト失敗時のCI通知の具体的な仕組みは未確定。
- ui.dbフィクスチャ(2.1節)の再生成スクリプト(`scripts/regenerate_ui_db_fixtures.py`)は実装済み(2.1節参照)。
- **UT-X-012/013の期待値は確定済み(2026-07-05)**: interrupt時のトランザクション状態遷移(design doc 6.2#9)の実機調査が完了し、明示的トランザクション内のinterruptは**トランザクションをabort状態にする**(design doc 6.3#10)ことが確定した。executorはタイムアウトinterrupt後にプローブ文でabortを検知し、`ROLLBACK`+`BEGIN` でトランザクションを再開して後続セルを継続する(abort系エラー処理への合流)。UT-X-012(interrupt成功時に後続処理を継続)の期待値はこの挙動と整合しており変更不要。UT-X-013(interrupt不能時の部分HTML+終了コード2)は後続セルの実行自体を断念する経路であり、トランザクション状態遷移に依存しないため同じく変更不要。前提はAT-011(6.1節)で恒久的にガードされる。

### 8.4 実DuckDBで再現不能なため恒常的にskipするケース

以下は前節までのブロック解除・スキーマ確定とは別に、**実DuckDBの挙動として確実に再現する手段がない**ために恒常的に `pytest.skip` される。

- **UT-X-013・UT-C-016**: interruptが既定30秒の猶予内に効かない(=un-interruptibleな)クエリを実DuckDBで確実かつ再現性をもって作り出す方法がない。interruptが効くかどうかはクエリの内部実装・実行段階に依存し、テストコードから意図的に「interruptが絶対に効かないクエリ」を発生させることを保証できないため、両テストとも `pytest.skip("cannot reliably reproduce un-interruptible query with real DuckDB")` として明示的にスキップする。8.3節のUT-X-012/013の期待値確定(トランザクション状態遷移 — 2026-07-05に確定済み)とは別の理由によるskipである点に留意。
