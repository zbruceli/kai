#include "face.h"

#include <vector>

namespace face {
namespace {

constexpr int W = 240, H = 135;
constexpr int EYE_L = 82, EYE_R = 158, EYE_Y = 52;
constexpr uint32_t FRAME_MS = 33;

// Reply view layout
constexpr int HEADER_H = 26;
constexpr int LINE_H = 18;
constexpr int BODY_Y = HEADER_H + 3;
constexpr int VISIBLE_LINES = (H - BODY_Y) / LINE_H;  // 5
constexpr int TEXT_X = 6;
constexpr int TEXT_W = W - TEXT_X - 10;               // room for the scrollbar

M5Canvas canvas(&M5.Display);
uint16_t KAI, DIM, RED_, SOFT, TRACK;

Expr expr = Expr::Offline;
float level = 0, shownLevel = 0;
String statusText;
int battery = -1;

// Reply state
struct Line {
  String text;
  bool fromCard;
};
Card replyCard;
bool replyHasCard = false;
String replyText;
std::vector<Line> lines;
bool linesDirty = false;
bool replyOn = false;
bool speaking = false;
int top = 0;              // first visible line
bool userScrolled = false;

uint32_t lastFrame = 0, nextBlink = 0, blinkUntil = 0, nextGaze = 0;
float gazeX = 0, gazeTarget = 0;

// ---- face view -------------------------------------------------------------

void eye(int cx, int cy, int w, int h, uint16_t color) {
  canvas.fillSmoothRoundRect(cx - w / 2, cy - h / 2, w, h, min(w, h) / 2, color);
}

void drawFace(uint32_t now) {
  int w = 30, h = 44, dy = 0;
  int dx = int(gazeX);

  switch (expr) {
    case Expr::Offline:
      for (int cx : {EYE_L, EYE_R}) {  // x_x
        canvas.drawWideLine(cx - 12, EYE_Y - 12, cx + 12, EYE_Y + 12, 4, DIM);
        canvas.drawWideLine(cx - 12, EYE_Y + 12, cx + 12, EYE_Y - 12, 4, DIM);
      }
      break;
    case Expr::Listening:
      w = 34, h = 52, dx = 0;  // wide-eyed and attentive
      break;
    case Expr::Thinking:
      h = 20, dy = -10, dx = 8;  // squinting up and to the side
      break;
    default:
      break;
  }
  if (expr != Expr::Offline) {
    if (now < blinkUntil && expr != Expr::Thinking) h = 5;
    eye(EYE_L + dx, EYE_Y + dy, w, h, KAI);
    eye(EYE_R + dx, EYE_Y + dy, w, h, KAI);
  }

  switch (expr) {
    case Expr::Listening: {
      if ((now / 400) % 2) canvas.fillCircle(W - 14, 12, 6, RED_);
      canvas.fillRoundRect(40, 102, 160, 8, 4, TRACK);
      canvas.fillRoundRect(40, 102, max(8, int(shownLevel * 160)), 8, 4, KAI);
      break;
    }
    case Expr::Thinking:
      for (int i = 0; i < 3; i++) {
        bool on = (now / 250) % 4 > uint32_t(i);
        canvas.fillCircle(104 + i * 16, 96, 4, on ? KAI : DIM);
      }
      break;
    case Expr::Idle:
      canvas.fillSmoothRoundRect(W / 2 - 10, 92, 20, 4, 2, DIM);  // resting mouth
      break;
    default:
      break;
  }

  if (statusText.length()) {
    canvas.setFont(&fonts::Font2);
    canvas.setTextColor(DIM);
    canvas.setTextDatum(bottom_center);
    canvas.drawString(statusText, W / 2, H - 2);
  }

  if (battery >= 0) {
    canvas.setFont(&fonts::Font0);
    canvas.setTextDatum(top_left);
    canvas.setTextColor(battery <= 15 ? RED_ : DIM);
    canvas.drawString(String(battery) + "%", 4, 4);
  }
}

// ---- reply view ------------------------------------------------------------

void wrapInto(const String& text, bool fromCard) {
  String line;
  int start = 0;
  const int len = text.length();
  while (start < len) {
    int end = text.indexOf(' ', start);
    if (end < 0) end = len;
    String word = text.substring(start, end);
    start = end + 1;
    if (word.isEmpty()) continue;
    String candidate = line.isEmpty() ? word : line + " " + word;
    if (canvas.textWidth(candidate) <= TEXT_W) {
      line = candidate;
      continue;
    }
    if (!line.isEmpty()) lines.push_back({line, fromCard});
    // A single word wider than the screen gets hard-broken.
    while (canvas.textWidth(word) > TEXT_W) {
      int cut = word.length() - 1;
      while (cut > 1 && canvas.textWidth(word.substring(0, cut)) > TEXT_W) cut--;
      lines.push_back({word.substring(0, cut), fromCard});
      word = word.substring(cut);
    }
    line = word;
  }
  if (!line.isEmpty()) lines.push_back({line, fromCard});
}

int maxTop() { return max(0, int(lines.size()) - VISIBLE_LINES); }

void rebuildLines() {
  canvas.setFont(&fonts::Font2);
  lines.clear();
  if (replyHasCard) {
    for (size_t i = 0; i < replyCard.count; i++) wrapInto(replyCard.lines[i], true);
  }
  wrapInto(replyText, false);
  linesDirty = false;
  // While Kai is talking, follow the newest words unless the user took over scrolling.
  top = (speaking && !userScrolled) ? maxTop() : min(top, maxTop());
}

void drawMiniFace(uint32_t now) {
  int eh = (now < blinkUntil) ? 3 : 12;
  canvas.fillSmoothRoundRect(8, 12 - eh / 2 - 3, 7, eh, 3, KAI);
  canvas.fillSmoothRoundRect(20, 12 - eh / 2 - 3, 7, eh, 3, KAI);
  if (speaking) {
    int mh = 2 + int(shownLevel * 6);
    canvas.fillSmoothRoundRect(12, 21 - mh / 2, 11, mh, 1, KAI);
  }
}

void drawReply(uint32_t now) {
  if (linesDirty) rebuildLines();

  drawMiniFace(now);
  canvas.setTextDatum(top_left);
  canvas.setFont(&fonts::FreeSansBold9pt7b);
  canvas.setTextColor(KAI);
  canvas.drawString(replyHasCard && replyCard.title.length() ? replyCard.title : String("Kai"), 36, 5);
  canvas.drawFastHLine(4, HEADER_H, W - 8, DIM);

  canvas.setFont(&fonts::Font2);
  for (int i = 0; i < VISIBLE_LINES && top + i < int(lines.size()); i++) {
    const Line& l = lines[top + i];
    canvas.setTextColor(l.fromCard ? TFT_WHITE : SOFT);
    canvas.drawString(l.text, TEXT_X, BODY_Y + i * LINE_H);
  }

  // Scrollbar, only when there's more than a screenful.
  if (int(lines.size()) > VISIBLE_LINES) {
    const int trackY = BODY_Y, trackH = H - BODY_Y - 3;
    const int thumbH = max(10, trackH * VISIBLE_LINES / int(lines.size()));
    const int thumbY = trackY + (trackH - thumbH) * top / max(1, maxTop());
    canvas.fillRoundRect(W - 5, trackY, 3, trackH, 1, TRACK);
    canvas.fillRoundRect(W - 5, thumbY, 3, thumbH, 1, KAI);
  }
}

}  // namespace

void begin() {
  M5.Display.setRotation(1);
  M5.Display.setBrightness(120);
  canvas.setColorDepth(16);
  canvas.createSprite(W, H);
  KAI = canvas.color565(90, 220, 255);
  DIM = canvas.color565(70, 90, 105);
  RED_ = canvas.color565(255, 70, 70);
  SOFT = canvas.color565(190, 215, 230);
  TRACK = canvas.color565(30, 40, 50);
}

void setExpr(Expr e) { expr = e; }
void setLevel(float l) { level = l; }
void setStatusText(const String& text) { statusText = text; }
void setBattery(int percent) { battery = percent; }

void clearReply() {
  replyHasCard = false;
  replyCard = Card{};
  replyText = "";
  lines.clear();
  top = 0;
  userScrolled = false;
  speaking = false;
}

void setReplyCard(const Card& card) {
  replyCard = card;
  replyHasCard = true;
  linesDirty = true;
}

void appendReplyText(const String& text) {
  replyText += text;
  linesDirty = true;
}

bool hasReply() { return replyHasCard || replyText.length() > 0; }
void showReply(bool on) { replyOn = on; }
bool replyVisible() { return replyOn; }
void setSpeaking(bool s) { speaking = s; }

void scrollReply() {
  if (linesDirty) rebuildLines();
  userScrolled = true;
  // Page down keeping one line of overlap for context; from the bottom, wrap to the top.
  top = (top >= maxTop()) ? 0 : min(top + VISIBLE_LINES - 1, maxTop());
}

void replyFinished() {
  speaking = false;
  if (!userScrolled) top = 0;
}

void render() {
  uint32_t now = millis();
  if (now - lastFrame < FRAME_MS) return;
  lastFrame = now;

  // Idle life: blinks and a slowly wandering gaze.
  if (now >= nextBlink) {
    blinkUntil = now + 120;
    nextBlink = now + 2500 + esp_random() % 3500;
  }
  if (now >= nextGaze) {
    gazeTarget = expr == Expr::Idle ? int(esp_random() % 17) - 8 : 0;
    nextGaze = now + 1500 + esp_random() % 3000;
  }
  gazeX += (gazeTarget - gazeX) * 0.15f;
  shownLevel += (level - shownLevel) * 0.5f;

  canvas.fillSprite(TFT_BLACK);
  if (replyOn) {
    drawReply(now);
  } else {
    drawFace(now);
  }
  canvas.pushSprite(0, 0);
}

}  // namespace face
