# Stock Researcher - Quantitative Rules Help & Reference Guide

This document provides a comprehensive summary and plain-language explanation for all **14 Quantitative Filtering Rules** used by the Stock Researcher Screener Pipeline.

---

## Stage 1: HTML Quantitative Rules (12 Rules)

### 1. PE vs Industry (`pe_vs_industry`)
- **Formula**: $\text{Stock P/E} \le 1.1 \times \text{Industry P/E}$
- **Meaning**: Valuation relative to peers. Ensures the stock is not excessively expensive compared to its industry group, allowing up to a 10% premium.

### 2. PE vs History (`pe_vs_historical`)
- **Formula**: $\text{Stock P/E} \le 1.1 \times \text{Historical P/E}$ for $\ge 2$ of 3 available periods (3Y, 5Y, 7Y).
- **Meaning**: Valuation relative to historical norm. Checks whether the stock's current price-to-earnings multiple is reasonable compared to its own past historical median multiples.

### 3. ROCE > 15% (`roce_over_15`)
- **Formula**: $\text{Return on Capital Employed (ROCE)} > 15\%$
- **Meaning**: Operating efficiency. Indicates that the business generates at least ₹15 of operating profit for every ₹100 of total capital employed in the business.

### 4. ROE > 15% (`roe_over_15`)
- **Formula**: $\text{Return on Equity (ROE)} > 15\%$
- **Meaning**: Shareholder profitability. Measures how efficiently management generates net profit using shareholders' equity capital.

### 5. Debt to Equity < 0.5 (`debt_to_equity_under_0_5`)
- **Formula**: $\frac{\text{Total Debt}}{\text{Total Equity}} < 0.5$
- **Meaning**: Solvency & leverage check. Confirms low financial risk with minimal debt burden (total debt must be less than half of total equity).

### 6. Pledged Shares = 0% (`pledged_zero`)
- **Formula**: $\text{Promoter Pledged Shares Percentage} = 0\%$
- **Meaning**: Promoter collateral safety. Guarantees promoters have not pledged their shareholding as collateral for loans, eliminating margin call liquidation risks.

### 7. Sales YoY & CAGR Growth (`sales_yoy_growth`)
- **Formula**: Market-Cap Tiered Revenue Thresholds:
  - **Large-Cap** ($> \text{₹}50,000\text{ Cr}$): Sales CAGR $\ge 10\%$, YoY Growth Floor $\ge 10\%$
  - **Mid-Cap** ($\text{₹}10,000 - 50,000\text{ Cr}$): Sales CAGR $\ge 12\%$, YoY Growth Floor $\ge 10\%$
  - **Small-Cap** ($< \text{₹}10,000\text{ Cr}$): Sales CAGR $\ge 15\%$, YoY Growth Floor $\ge 12\%$
  - *YoY Hit Rate*: At least $60\%$ of annual growth observations must meet or exceed the YoY Growth Floor.
- **Meaning**: Consistent revenue expansion. Evaluates top-line growth using tier-adjusted criteria so large-cap bluechips are not unfairly penalized by small-cap growth benchmarks.

### 8. Profit Growth > 10% (`profit_growth_over_10`)
- **Formula**: $\text{Compounded Profit Growth} > 10\%$ for $\ge 2$ of 4 periods (10Y, 5Y, 3Y, TTM).
- **Meaning**: Earnings growth consistency. Confirms that net profit is growing at a healthy double-digit pace across multiple historical time horizons.

### 9. Stock CAGR < Profit Growth (`stock_cagr_below_profit_growth`)
- **Formula**: $\text{Stock Price CAGR} < \text{Profit Growth}$ for $\ge 2$ matching periods (10Y, 5Y, 3Y, TTM).
- **Meaning**: Multiple expansion guard. Ensures stock price growth has not outpaced underlying profit growth, avoiding valuation bubble risks.

### 10. Promoter Decrease < 5% (`promoter_holding_decrease_under_5`)
- **Formula**: $\text{Initial Promoter \%} - \text{Latest Promoter \%} < 5\%$
- **Meaning**: Insider skin-in-the-game. Confirms that company founders and key promoters have maintained their stake over time without dumping shares.

### 11. PEG Ratio <= 1.5 (`peg_ratio_under_1_5`)
- **Formula**: $\frac{\text{Stock P/E}}{\text{Effective Profit CAGR (Avg 3Y \& 5Y)}} \le 1.5$
- **Meaning**: Growth-adjusted valuation (Peter Lynch Rule). Ensures you do not overpay for earnings relative to the company's profit growth rate.

### 12. Revenue Quality Guard (`revenue_quality_guard`)
- **Formula**: $\text{Profit CAGR (3Y)} \ge \text{Sales CAGR (3Y)}$ **OR** $\text{Profit CAGR (5Y)} \ge \text{Sales CAGR (5Y)}$
- **Meaning**: Operating leverage & margin preservation. Confirms that profit growth matches or exceeds sales growth over 3Y or 5Y, indicating expanding or stable profit margins.

---

## Stage 2: Excel Financial Rules (2 Rules)

### 13. Rule 12: SSGR (`rule_12_ssgr`)
- **Formula**: $\text{3Y Avg Self-Sustained Growth Rate} > 10\%$ AND $\text{3Y Avg SSGR} \ge \text{3Y Sales CAGR}$
- **Meaning**: Self-funded growth capacity. Tests whether the company can fund its sales growth purely through internal cash retention without needing debt or diluting equity.

### 14. Rule 13: CFO / EBITDA & CFO / PAT (`rule_13_cfo_ebitda`)
- **Formula**: $\frac{\text{5Y Cum. CFO}}{\text{5Y Cum. EBITDA}} \ge 65\%$ AND $\frac{\text{5Y Cum. CFO}}{\text{5Y Cum. PAT}} \ge 80\%$
- **Meaning**: Cash flow conversion quality. Ensures reported accounting profits actually convert into real operating cash flow rather than getting trapped in unsold inventory or unpaid receivables.
