# fxbot(OANDA practice 専用・日次)

資金3万円・最大25倍の想定で、**OANDA practice(デモ)口座だけ**で動く日次FXボット。
実弾(本番口座)は対象外で、本番URLはコードで拒否する。

> **現状: 運用開始は保留。** 注文経路(OANDA 実API)の検証がまだ済んでいない。
> 下の「運用開始までの手順」の 1〜4 を人間が終えるまで、ワークフローは dry-run のまま使うこと。

## 絶対に変えないこと(テストで固定)
- `fxbot/policy.py`: `live_trading=False`、`max_leverage=25`(上限)、`max_drawdown_30d=-0.25`
- 30日ドローダウンが -25% に達したら全決済し、`state/HALT` を作って永続停止
- HALT の解除は人間だけ(`python -m fxbot.unhalt`。解除処理は unhalt.py 以外に存在しない)
- 本番URL(`api-fxtrade`)は `BrokerRefused` で拒否。許可するのは `https://api-fxpractice.oanda.com` だけ

## 構成
| ファイル | 役割 |
|---|---|
| `fxbot/data.py` | 日足取得(ECB参照レート[既定] / Stooq)。失敗時は `DataError` で停止 |
| `fxbot/strategy.py` | 60日モメンタム + 効率比フィルタ(er_min)+ ボラ目標(target_vol)。1000通貨に切り捨て。損切り幅 = 3×ATR |
| `fxbot/backtest.py`, `walkforward.py` | 3万円・1000通貨丸め・キルスイッチ込みのバックテストと、ウォークフォワード検証 |
| `fxbot/broker.py` | `OandaPracticeBroker`(practice のみ、SL必須、clientExtensions ID で冪等)/ `PaperBroker` |
| `fxbot/runner.py` | 日次の処理本体(下記) |
| `fxbot/notify.py` | Discord 通知と、外部 heartbeat の ping |
| `fxbot/heartbeat.py` | runner の停止を検知 |
| `fxbot/smoke.py` | practice 実API のスモークテスト(**人間が手動で実行**) |
| `fxbot/unhalt.py` | HALT 解除(人間専用。対話端末で確認文の入力が必須) |
| `fxbot/lot_analysis.py` | 最小ロットと資本の切り分け分析(分析専用) |
| `state/state.json`, `state/HALT`, `state/INITIALIZED` | 実行状態(runner が commit/push する) |
| `config/params.json` | 採用されたパラメータ(不採用なら現行のまま) |
| `reports/` | 検証レポート |

## runner の処理順
1. ポリシー検証
2. HALT の確認(HALT ファイルか `state.halted` があれば何もしない)
3. state の検証(fail-closed)
4. データ取得
5. 口座の照合(円口座であること・同じ口座であること・対象外の建玉が無いこと)
6. キルスイッチ判定(dry-run でも決済と停止は実行する)
7. 目標ポジションの計算
8. 差分注文(減らす注文を先に出す。反転は「0まで減らす」と「新規」に分割。新規には必ずSLを付ける)
9. 注文後の建玉をブローカーから取り直して state に保存
10. `--git` 指定時は commit/push

- **運用資産**は「元本3万円 + 開始以降の口座損益」(上限は口座NAV)。デモ口座の残高(数百万円等)は使わない。
- **入出金の扱い**: 建玉ゼロの間に NAV が変わった場合は入出金とみなし、損益として扱わない(デモの残高リセット対策)。
- **発注の二重ロック**: `--dry-run` を付けない **かつ** 環境変数 `FXBOT_PRACTICE_ORDERS=true` のときだけ注文する。
- **冪等性**: clientExtensions ID は `fxbot-日付-ペア-数量`。同じ ID の注文があれば再送しない。タイムアウト時も再送せず、先に注文履歴を照合する。
- **fail-closed**: 次の場合は新規注文を出さず、通知して停止する(HALT は維持する)。
  - state.json が壊れている、または必須項目が欠けている
  - 初期化済み(`INITIALIZED` あり)なのに state.json が無い
  - 古い state のまま push しようとして衝突した(上書きしない)
- **初期化**は `--init` を付けたときだけ。初期化済みの状態で `--init` を付けても拒否する。

## 運用開始までの手順(人間が行う)
1. **practice 口座の API キーを発行する**(出金権限のないものを前提にする。v20 API には出金のエンドポイントが無い)。
2. **GitHub Secrets / 変数を登録する**(Settings → Secrets and variables → Actions)。
   - Secrets(必須): `OANDA_API_KEY`、`OANDA_ACCOUNT_ID`、`DISCORD_WEBHOOK_URL`
   - Secrets(任意): `HEALTHCHECK_URL`(healthchecks.io などの ping URL)
   - Variables: `FXBOT_PRACTICE_ORDERS`。**最初は設定しない**(=dry-run)
3. **スモークテストを実行する**(手元の端末で。CI からは実行できない)。
   ```bash
   cd fxbot && pip install -r requirements.txt
   export OANDA_API_KEY=... OANDA_ACCOUNT_ID=...
   python -m fxbot.smoke --confirm-practice --pair USD_JPY
   ```
   - 中身: 口座サマリ → 最小取引単位 → 最小単位でSL付きの成行注文 → 注文履歴でIDを照合 → その建玉だけ決済 → 建玉が元に戻ったことを確認。
   - 結果は「未確認(人間が実行)」のまま。実行したら `reports/` に記録すること。
4. **main にマージする。** スケジュール実行は既定ブランチ(main)のワークフローでしか動かない。マージするまで日次ジョブも heartbeat も動かない。
5. **dry-run で数日動かす。** 状態は「未初期化」となり、データ取得と口座接続の確認だけが行われる。
6. **初期化する。** Actions → fxbot-daily → Run workflow で `init` にチェックを入れて実行する。
7. **発注を有効にする。** 変数 `FXBOT_PRACTICE_ORDERS=true` を設定する。外すと次回から dry-run に戻る。

## 監視
- runner は失敗時・安全停止時・キルスイッチ発動時・git push 失敗時に Discord へ通知する(`--require-webhook` で Webhook 未設定を拒否)。
- `fxbot-heartbeat`(6時間ごと)は `state.json` の `last_run` が30時間より古ければ通知し、ジョブを失敗させる。
  - 初回実行前(state も INITIALIZED も無い間)は誤報しない(猶予)。
  - 初期化済みなのに state が無い、または壊れている場合は異常として通知する。
- **GitHub 内の heartbeat だけでは不十分。** GitHub のスケジュール自体が止まると、heartbeat も一緒に止まる。リポジトリに60日間動きが無いとスケジュールは自動で無効化されるが、runner は毎日 state を commit するので通常は起きない。それでも外部の `HEALTHCHECK_URL` を併用すること。runner は開始・成功・失敗を ping する。ping が失敗しても取引判断には影響させず、通知だけ行う。

## HALT の解除(人間のみ)
```bash
cd fxbot
cat state/HALT                 # 理由を確認
python -m fxbot.unhalt         # 対話端末で確認文を入力。CI など非TTYでは常に拒否
git add state && git commit -m "fxbot: unhalt (human)" && git push
```

## 既知の制限
- **データ**: Stooq は無人取得に JavaScript のブラウザ確認を返すため使えない(回避はしない)。既定は ECB 参照レートで、**1日1点(14:15 CET)・高値と安値なし**。
  - 戦略は終値しか使わない(高値・安値は未使用)。
  - 損切り幅の ATR は |終値の前日差| の20日平均で近似している。真の ATR(高値・安値を使う)より小さめに出る可能性がある(推測)ので、3倍して使っている。
  - ECB の休日にはデータが古くなる。5日より古ければ `DataError` で停止する。
- **ロット制約**: 3万円・1000通貨単位では、1000通貨を1つ持つだけでレバ約3.5〜7倍・1ATR逆行で約1.3〜2%の損失になる。現行パラメータでは丸めで**取引はほぼ発生しない**(`reports/lot_constraint_analysis.md`)。
- **検証結果**: ウォークフォワードの結果は不採用で、パラメータは現行のまま(`reports/walkforward_*.md`)。バックテストは損切りを再現していない。
- **リポジトリの公開範囲**: 公開リポジトリだと、state.json(運用資産・建玉)が誰でも見られる。**非公開を推奨。**
- **OANDA 実API**: モックでしかテストしていない。発注・決済・建玉照会の実挙動は、スモークテスト(人間が実行)で確認するまで未確認。

## 開発
```bash
cd fxbot && pip install -r requirements.txt
python -m pytest -q
python -m fxbot.walkforward --offline     # data_cache/ を使う。--offline なしだと ECB から取得
python -m fxbot.lot_analysis --offline
python -m fxbot.runner --paper --dry-run  # ローカル確認(state/ に書き込むので注意)
```
