// ---------------------------------------------------------------
// api.js
// Anthropic (Claude) APIへ食事写真を送り、栄養素を推定してもらう部分。
//
// このアプリはブラウザから直接 Anthropic API を呼び出します。
// そのためAPIキーはこの端末のブラウザ内(localStorage)にのみ保存され、
// どこか外部のサーバーには送信されません（Anthropic以外には）。
// ただし「ブラウザから直接APIキーを使う」方式は、そのブラウザ環境に
// アクセスできる人にはキーが見える形になります。共有PCなどでは
// 使用後にキーを削除することをおすすめします。
// ---------------------------------------------------------------

const ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages";
const ANTHROPIC_VERSION = "2023-06-01";

const MODEL_OPTIONS = [
  { value: "claude-sonnet-5", label: "Claude Sonnet 5(標準・おすすめ)" },
  { value: "claude-haiku-4-5", label: "Claude Haiku 4.5(高速・低コスト)" },
  { value: "claude-opus-5", label: "Claude Opus 5(高精度・低速)" },
];

const ANALYSIS_PROMPT = `あなたは経験豊富な管理栄養士です。添付された画像を見て、次の手順で栄養価を求めてください。

【手順】
1. まず、画像の中に「栄養成分表示」「成分表(目安)」のような、エネルギー・たんぱく質・脂質・炭水化物・食塩相当量などの数値が印刷/表示された表やラベルが写っていないか確認してください。
   これは実際の料理の写真そのものだけでなく、宅配弁当・社食・コンビニ食品のアプリ画面やパッケージのスクリーンショットである場合も含みます。
2. そのような数値表示が見つかった場合は、あなた自身で推定するのではなく、そこに書かれている数値をできるだけ正確に読み取って使ってください。表示されていない栄養素(食物繊維・ビタミン・ミネラル類など)だけ、料理の内容から一般的な値を推定してください。この場合 "source" は "label" にしてください。
3. そのような数値表示が見つからない場合は、写っている料理・食品を特定し、写真全体（1食分）の栄養価をあなたの知識から推定してください。この場合 "source" は "estimate" にしてください。

必ず次のJSON形式のみで出力してください。前置きや説明文、コードブロックの記法(\`\`\`)は一切つけないでください。

{
  "items": [ { "name": "料理名や食品名", "amount": "推定量(例: 茶碗1杯・150g など)" } ],
  "nutrients": {
    "calories": 数値(kcal),
    "protein": 数値(g),
    "fat": 数値(g),
    "carb": 数値(g),
    "fiber": 数値(g),
    "salt": 数値(g、食塩相当量),
    "saturatedFat": 数値(g、飽和脂肪酸),
    "calcium": 数値(mg),
    "iron": 数値(mg),
    "zinc": 数値(mg、亜鉛),
    "magnesium": 数値(mg、マグネシウム),
    "potassium": 数値(mg、カリウム),
    "vitaminA": 数値(µgRAE),
    "vitaminC": 数値(mg),
    "vitaminD": 数値(µg),
    "vitaminE": 数値(mg),
    "vitaminB1": 数値(mg),
    "vitaminB2": 数値(mg),
    "vitaminB6": 数値(mg),
    "vitaminB12": 数値(µg),
    "folate": 数値(µg、葉酸)
  },
  "source": "label" または "estimate",
  "confidence": "high" または "medium" または "low",
  "note": "写真から判断しづらかった点があれば一言(なければ空文字)"
}

数値はすべて画像に対応する食事全体の合計値とし、単位はすべて上記の通りにしてください。分からない栄養素も、一般的な料理の標準的な値から最善の推定値を入れてください（0にしないでください）。`;

class ApiKeyError extends Error {}
class ApiRequestError extends Error {}
class ApiParseError extends Error {}

// 画像をリサイズ・圧縮してJPEGのbase64(データ部分のみ)を返す
function resizeImageToBase64(file, maxDim = 1024, quality = 0.82) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(new Error("画像の読み込みに失敗しました"));
    reader.onload = () => {
      const img = new Image();
      img.onerror = () => reject(new Error("画像の読み込みに失敗しました"));
      img.onload = () => {
        let { width, height } = img;
        if (width > maxDim || height > maxDim) {
          if (width > height) {
            height = Math.round((height * maxDim) / width);
            width = maxDim;
          } else {
            width = Math.round((width * maxDim) / height);
            height = maxDim;
          }
        }
        const canvas = document.createElement("canvas");
        canvas.width = width;
        canvas.height = height;
        const ctx = canvas.getContext("2d");
        ctx.drawImage(img, 0, 0, width, height);
        const dataUrl = canvas.toDataURL("image/jpeg", quality);
        resolve(dataUrl); // "data:image/jpeg;base64,xxxx"
      };
      img.src = reader.result;
    };
    reader.readAsDataURL(file);
  });
}

function dataUrlToParts(dataUrl) {
  const m = /^data:([^;]+);base64,(.*)$/.exec(dataUrl);
  if (!m) throw new Error("画像形式の変換に失敗しました");
  return { mediaType: m[1], base64: m[2] };
}

// AIの返答からJSON部分だけを取り出す（前後に文章が混ざっても耐えるように）
function extractJson(text) {
  const start = text.indexOf("{");
  const end = text.lastIndexOf("}");
  if (start === -1 || end === -1 || end < start) {
    throw new ApiParseError("AIの応答からJSONを取り出せませんでした");
  }
  const jsonStr = text.slice(start, end + 1);
  try {
    return JSON.parse(jsonStr);
  } catch (e) {
    throw new ApiParseError("AIの応答の解析に失敗しました");
  }
}

async function analyzeFoodPhoto({ dataUrl, apiKey, model }) {
  if (!apiKey) throw new ApiKeyError("APIキーが設定されていません");
  const { mediaType, base64 } = dataUrlToParts(dataUrl);

  let resp;
  try {
    resp = await fetch(ANTHROPIC_API_URL, {
      method: "POST",
      headers: {
        "content-type": "application/json",
        "x-api-key": apiKey,
        "anthropic-version": ANTHROPIC_VERSION,
        "anthropic-dangerous-direct-browser-access": "true",
      },
      body: JSON.stringify({
        model: model || "claude-sonnet-5",
        max_tokens: 1536,
        messages: [
          {
            role: "user",
            content: [
              { type: "image", source: { type: "base64", media_type: mediaType, data: base64 } },
              { type: "text", text: ANALYSIS_PROMPT },
            ],
          },
        ],
      }),
    });
  } catch (e) {
    throw new ApiRequestError("ネットワークエラー: APIに接続できませんでした");
  }

  if (!resp.ok) {
    let detail = "";
    try {
      const errJson = await resp.json();
      detail = errJson?.error?.message || "";
    } catch (e) {
      /* ignore */
    }
    if (resp.status === 401) {
      throw new ApiKeyError("APIキーが正しくないようです。設定を確認してください。");
    }
    if (resp.status === 429) {
      throw new ApiRequestError("APIの利用上限に達しました。しばらく待ってから再度お試しください。");
    }
    throw new ApiRequestError(`APIエラー(${resp.status}): ${detail || "しばらくしてから再度お試しください"}`);
  }

  const data = await resp.json();
  const text = (data.content || []).map((b) => b.text || "").join("");
  const parsed = extractJson(text);

  const nutrients = {};
  Object.keys(NUTRIENT_META).forEach((k) => {
    const v = Number(parsed?.nutrients?.[k]);
    nutrients[k] = Number.isFinite(v) ? Math.round(v * 10) / 10 : 0;
  });

  return {
    items: Array.isArray(parsed.items) ? parsed.items : [],
    nutrients,
    source: parsed.source === "label" ? "label" : "estimate",
    confidence: parsed.confidence || "medium",
    note: parsed.note || "",
  };
}
