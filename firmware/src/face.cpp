#include "face.h"

namespace face {
namespace {

constexpr int W = 240, H = 135;
constexpr int EYE_L = 82, EYE_R = 158, EYE_Y = 52;
constexpr uint32_t FRAME_MS = 33;

M5Canvas canvas(&M5.Display);
uint16_t KAI, DIM, RED_, BG;

Expr expr = Expr::Offline;
float level = 0, shownLevel = 0;
String caption, statusText;
int battery = -1;
Card card;
bool cardOn = false;

uint32_t lastFrame = 0, nextBlink = 0, blinkUntil = 0, nextGaze = 0;
float gazeX = 0, gazeTarget = 0;

void eye(int cx, int cy, int w, int h, uint16_t color) {
  canvas.fillSmoothRoundRect(cx - w / 2, cy - h / 2, w, h, min(w, h) / 2, color);
}

void drawEyes(uint32_t now) {
  int w = 30, h = 44, dy = 0;
  float dx = gazeX;
  uint16_t color = KAI;

  switch (expr) {
    case Expr::Offline:
      for (int cx : {EYE_L, EYE_R}) {  // x_x
        canvas.drawWideLine(cx - 12, EYE_Y - 12, cx + 12, EYE_Y + 12, 4, DIM);
        canvas.drawWideLine(cx - 12, EYE_Y + 12, cx + 12, EYE_Y - 12, 4, DIM);
      }
      return;
    case Expr::Listening:
      w = 34, h = 52;  // wide-eyed and attentive
      dx = 0;
      break;
    case Expr::Thinking:
      h = 20, dy = -10, dx = 8;  // squinting up and to the side
      break;
    default:
      break;
  }
  if (now < blinkUntil && expr != Expr::Thinking) h = 5;
  eye(EYE_L + dx, EYE_Y + dy, w, h, color);
  eye(EYE_R + dx, EYE_Y + dy, w, h, color);
}

void drawFace(uint32_t now) {
  drawEyes(now);

  switch (expr) {
    case Expr::Listening: {
      if ((now / 400) % 2) canvas.fillCircle(W - 14, 12, 6, RED_);
      int bar = int(shownLevel * 160);
      canvas.fillRoundRect(40, 102, 160, 8, 4, canvas.color565(30, 40, 50));
      canvas.fillRoundRect(40, 102, max(8, bar), 8, 4, KAI);
      break;
    }
    case Expr::Thinking:
      for (int i = 0; i < 3; i++) {
        bool on = (now / 250) % 4 > uint32_t(i);
        canvas.fillCircle(104 + i * 16, 96, 4, on ? KAI : DIM);
      }
      break;
    case Expr::Speaking: {
      int mh = 4 + int(shownLevel * 22);
      canvas.fillSmoothRoundRect(W / 2 - 18, 94 - mh / 2, 36, mh, min(mh / 2, 10), KAI);
      break;
    }
    case Expr::Idle:
      canvas.fillSmoothRoundRect(W / 2 - 10, 92, 20, 4, 2, DIM);  // resting mouth
      break;
    default:
      break;
  }

  const String& bottom = (expr == Expr::Speaking && caption.length()) ? caption : statusText;
  if (bottom.length()) {
    canvas.setFont(&fonts::Font2);
    canvas.setTextColor(DIM);
    canvas.setTextDatum(bottom_center);
    // Show the tail that fits on one line.
    String text = bottom;
    while (text.length() > 1 && canvas.textWidth(text) > W - 8) text.remove(0, 1);
    canvas.drawString(text, W / 2, H - 2);
  }
}

void drawCard() {
  canvas.setTextDatum(top_left);
  canvas.setFont(&fonts::FreeSansBold9pt7b);
  canvas.setTextColor(KAI);
  canvas.drawString(card.title, 6, 4);
  canvas.fillSmoothRoundRect(W - 30, 6, 7, 12, 3, KAI);  // tiny Kai peeking in the corner
  canvas.fillSmoothRoundRect(W - 18, 6, 7, 12, 3, KAI);
  canvas.drawFastHLine(6, 24, W - 12, DIM);
  canvas.setFont(&fonts::Font2);
  canvas.setTextColor(TFT_WHITE);
  for (size_t i = 0; i < card.count; i++) canvas.drawString(card.lines[i], 6, 30 + i * 21);
}

void drawBattery() {
  if (battery < 0 || cardOn) return;
  canvas.setFont(&fonts::Font0);
  canvas.setTextDatum(top_left);
  canvas.setTextColor(battery <= 15 ? RED_ : DIM);
  canvas.drawString(String(battery) + "%", 4, 4);
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
  BG = TFT_BLACK;
}

void setExpr(Expr e) {
  if (e != expr) caption = "";
  expr = e;
}
void setLevel(float l) { level = l; }
void setCaption(const String& text) { caption = text; }
void setStatusText(const String& text) { statusText = text; }
void setBattery(int percent) { battery = percent; }
void showCard(const Card& c) { card = c; cardOn = true; }
void hideCard() { cardOn = false; }
bool cardVisible() { return cardOn; }

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

  canvas.fillSprite(BG);
  if (cardOn) {
    drawCard();
  } else {
    drawFace(now);
  }
  drawBattery();
  canvas.pushSprite(0, 0);
}

}  // namespace face
