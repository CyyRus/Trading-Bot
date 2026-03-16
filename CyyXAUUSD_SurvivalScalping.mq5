#property copyright "Cyy XAUUSD Survival Scalping Bot"
#property version   "3.4"
#property strict

#include <Trade\Trade.mqh>

//─── Inputs ──────────────────────────────────────────────────────────────────
input double LotSize           = 0.005;   // Lot size
input int    StopLossPips      = 30;      // Stop loss in pips (reduced: ~$1.50 risk on 0.005 lot)
input double RiskRewardRatio   = 1.8;     // TP = SL * RR
input int    MaxOpenTrades     = 1;       // Max simultaneous positions
input int    CooldownMinutes   = 15;      // Minutes between trades
input double TargetBalance     = 5000.0;  // Stop trading at this balance
input int    MagicNumber       = 12345;

input double MaxSpreadPoints   = 50;      // Max allowed spread (points)
input string AllowedSymbol     = "XAUUSD";
input int    H1TrendMAPeriod   = 50;      // H1 EMA period for trend filter
input int    ConfirmCandles    = 2;       // Breakout confirmation candles
input int    PullbackPips      = 25;      // Required pullback depth (pips)
input bool   EnableVolPause    = false;   // Pause on volatile hour edges

//─── Globals ─────────────────────────────────────────────────────────────────
CTrade   trade;
datetime lastTradeTime  = 0;
datetime lastAlivePrint = 0;
datetime lastDiagBar    = 0;   // tracks last M5 bar for diagnostic logging
int      h1MAHandle     = INVALID_HANDLE;

//─── OnInit ──────────────────────────────────────────────────────────────────
int OnInit()
{
   if(AllowedSymbol != "" && _Symbol != AllowedSymbol)
   {
      Print("ERROR: EA designed for ", AllowedSymbol, " but attached to ", _Symbol);
      return INIT_FAILED;
   }

   trade.SetExpertMagicNumber(MagicNumber);

   h1MAHandle = iMA(_Symbol, PERIOD_H1, H1TrendMAPeriod, 0, MODE_EMA, PRICE_CLOSE);
   if(h1MAHandle == INVALID_HANDLE)
   {
      Print("ERROR: Failed to create H1 MA handle");
      return INIT_FAILED;
   }

   lastAlivePrint = TimeCurrent();   // prevent immediate heartbeat on startup

   Print("=================================================================");
   Print("Cyy Scalping Bot v3.4 | Symbol: ", _Symbol);
   Print("Balance : ", DoubleToString(AccountInfoDouble(ACCOUNT_BALANCE), 2));
   Print("Equity  : ", DoubleToString(AccountInfoDouble(ACCOUNT_EQUITY),  2));
   Print("Vol pause: ", EnableVolPause ? "ON" : "OFF");
   Print("=================================================================");
   return INIT_SUCCEEDED;
}

//─── OnDeinit ────────────────────────────────────────────────────────────────
void OnDeinit(const int reason)
{
   if(h1MAHandle != INVALID_HANDLE) IndicatorRelease(h1MAHandle);
   Print("Bot stopped | Reason: ", reason,
         " | Balance: ", DoubleToString(AccountInfoDouble(ACCOUNT_BALANCE), 2));
}

//─── Helpers ─────────────────────────────────────────────────────────────────
int CountOpenTrades()
{
   int count = 0;
   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      if(PositionGetTicket(i) > 0 &&
         PositionGetInteger(POSITION_MAGIC) == MagicNumber)
         count++;
   }
   return count;
}

bool IsCooldownActive()
{
   if(lastTradeTime == 0) return false;
   int secondsLeft = (CooldownMinutes * 60) - (int)(TimeCurrent() - lastTradeTime);
   if(secondsLeft > 0)
   {
      static datetime lastLog = 0;
      if(TimeCurrent() - lastLog >= 60)
      {
         Print("Cooldown: ", secondsLeft, "s remaining");
         lastLog = TimeCurrent();
      }
      return true;
   }
   return false;
}

bool IsVolatileTime()
{
   if(!EnableVolPause) return false;
   MqlDateTime tm;
   TimeToStruct(TimeCurrent(), tm);
   if(tm.min < 2 || tm.min > 58)
   {
      static datetime lastLog = 0;
      if(TimeCurrent() - lastLog >= 60)
      {
         Print("Volatile hour edge — pausing");
         lastLog = TimeCurrent();
      }
      return true;
   }
   return false;
}

bool IsSpreadAcceptable()
{
   double point = SymbolInfoDouble(_Symbol, SYMBOL_POINT);
   if(point == 0) return false;
   double spreadPoints = (SymbolInfoDouble(_Symbol, SYMBOL_ASK) -
                          SymbolInfoDouble(_Symbol, SYMBOL_BID)) / point;
   if(spreadPoints > MaxSpreadPoints)
   {
      static datetime lastLog = 0;
      if(TimeCurrent() - lastLog >= 60)
      {
         Print("Spread too wide: ", DoubleToString(spreadPoints, 1), " pts");
         lastLog = TimeCurrent();
      }
      return false;
   }
   return true;
}

//─── Strategy ────────────────────────────────────────────────────────────────
// Returns  1 if the last ConfirmCandles (starting at index 2) are all bullish,
//         -1 if all bearish, 0 otherwise.
// Candle index 1 is reserved as the pullback candle and is NOT counted here.
int GetBreakoutSignal()
{
   int bulls = 0, bears = 0;
   int startIdx = 2;
   int endIdx   = startIdx + ConfirmCandles - 1;

   for(int i = startIdx; i <= endIdx; i++)
   {
      double o = iOpen (_Symbol, PERIOD_M5, i);
      double c = iClose(_Symbol, PERIOD_M5, i);
      if(c > o) bulls++;
      else if(c < o) bears++;
   }

   if(bulls == ConfirmCandles) return  1;
   if(bears == ConfirmCandles) return -1;
   return 0;
}

// Returns the H1 trend: 1 = bullish (price > EMA), -1 = bearish, 0 = unknown.
int GetH1TrendDirection()
{
   double ma[];
   ArraySetAsSeries(ma, true);
   if(CopyBuffer(h1MAHandle, 0, 0, 1, ma) <= 0) return 0;

   double bid = SymbolInfoDouble(_Symbol, SYMBOL_BID);
   if(bid > ma[0]) return  1;
   if(bid < ma[0]) return -1;
   return 0;
}

// Returns true when candle 1 (most recent closed candle) has pulled back
// against the breakout direction by at least PullbackPips from candle 2
// (the last breakout candle). Candles 1 and 2 are separate from the
// ConfirmCandles breakout window (indices 2..2+ConfirmCandles-1), so there
// is no overlap between breakout detection and pullback detection.
bool IsPullbackAfterBreakout(int signal)
{
   double pip           = SymbolInfoDouble(_Symbol, SYMBOL_POINT) * 10;
   double close1        = iClose(_Symbol, PERIOD_M5, 1);   // pullback candle
   double closeBreakout = iClose(_Symbol, PERIOD_M5, 2);   // last breakout candle

   if(signal ==  1) return (closeBreakout - close1) > PullbackPips * pip;
   if(signal == -1) return (close1 - closeBreakout) > PullbackPips * pip;
   return false;
}

//─── OnTick ──────────────────────────────────────────────────────────────────
void OnTick()
{
   // Hourly heartbeat
   datetime now = TimeCurrent();
   if(now - lastAlivePrint >= 3600)
   {
      Print("Alive | Balance: ", DoubleToString(AccountInfoDouble(ACCOUNT_BALANCE), 2),
            " | Equity: ",       DoubleToString(AccountInfoDouble(ACCOUNT_EQUITY),  2),
            " | Open: ",         CountOpenTrades());
      lastAlivePrint = now;
   }

   // Target check
   double balance = AccountInfoDouble(ACCOUNT_BALANCE);
   if(balance >= TargetBalance)
   {
      Print("TARGET REACHED | Balance: ", DoubleToString(balance, 2));
      ExpertRemove();
      return;
   }

   // Pre-trade guards
   if(CountOpenTrades()  >= MaxOpenTrades) return;
   if(IsVolatileTime())                    return;
   if(IsCooldownActive())                  return;
   if(!IsSpreadAcceptable())               return;

   // Signal pipeline — with per-candle diagnostic logging
   int signal   = GetBreakoutSignal();
   int h1Trend  = GetH1TrendDirection();
   bool pullback = (signal != 0) && IsPullbackAfterBreakout(signal);

   datetime barTime = iTime(_Symbol, PERIOD_M5, 1);
   if(barTime != lastDiagBar)
   {
      lastDiagBar = barTime;
      string why = "WAITING";
      if     (signal == 0)              why = "No breakout (candles mixed)";
      else if(h1Trend != signal)        why = "H1 trend mismatch (breakout=" + (string)signal + " H1=" + (string)h1Trend + ")";
      else if(!pullback)                why = "Pullback too small (<" + (string)PullbackPips + " pips)";
      else                              why = "ALL CLEAR — placing trade";
      Print("Diag | breakout=", signal, " H1=", h1Trend, " pullback=", pullback, " | ", why);
   }

   if(signal == 0)              return;
   if(h1Trend != signal)        return;
   if(!pullback)                return;

   // Calculate SL / TP distances
   double point      = SymbolInfoDouble(_Symbol, SYMBOL_POINT);
   if(point == 0) return;
   double slDist = StopLossPips                    * point * 10;
   double tpDist = StopLossPips * RiskRewardRatio  * point * 10;

   double ask = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
   double bid = SymbolInfoDouble(_Symbol, SYMBOL_BID);
   bool   ok  = false;

   if(signal == 1)
   {
      double sl = NormalizeDouble(ask - slDist, _Digits);
      double tp = NormalizeDouble(ask + tpDist, _Digits);
      ok = trade.Buy(LotSize, _Symbol, ask, sl, tp, "Pullback Buy");
      if(ok)
         Print("BUY  | entry: ", ask, " | SL: ", sl, " | TP: ", tp);
      else
         Print("BUY failed | error: ", GetLastError());
   }
   else
   {
      double sl = NormalizeDouble(bid + slDist, _Digits);
      double tp = NormalizeDouble(bid - tpDist, _Digits);
      ok = trade.Sell(LotSize, _Symbol, bid, sl, tp, "Pullback Sell");
      if(ok)
         Print("SELL | entry: ", bid, " | SL: ", sl, " | TP: ", tp);
      else
         Print("SELL failed | error: ", GetLastError());
   }

   if(ok) lastTradeTime = TimeCurrent();
}
