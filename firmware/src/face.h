// Kai's face and info cards on the 240x135 screen, drawn into an off-screen canvas to avoid flicker.
#pragma once
#include <M5Unified.h>

enum class Expr { Offline, Idle, Listening, Thinking, Speaking };

constexpr size_t CARD_LINES = 5;

struct Card {
  String title;
  String lines[CARD_LINES];
  size_t count = 0;
};

namespace face {

void begin();
void setExpr(Expr e);
void setLevel(float level);             // mic meter while listening, mouth while speaking
void setCaption(const String& text);    // tail of what Kai is saying
void setStatusText(const String& text); // small hint under the face (e.g. "connecting wifi")
void setBattery(int percent);
void showCard(const Card& card);        // replaces the face until hideCard()
void hideCard();
bool cardVisible();
void render();                          // call every loop; throttles itself to ~30 fps

}  // namespace face
