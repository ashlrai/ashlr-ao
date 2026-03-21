# Ashlr.ai -- Automated Upwork Job Monitoring & Triage System

## Executive Summary

This guide covers setting up automated monitoring for $5K-$50K AI development gigs on Upwork, comparing the major tools, and designing an AI-powered triage pipeline using Claude.

---

## Part 1: Tool Comparison

### 1. Upwork Built-in Alerts (Free)

- **Speed**: 30-60 minute delay (often hours). Unreliable.
- **Filters**: Basic keyword, category, budget range. No client quality filters.
- **Integrations**: Email only (no Slack/Telegram/webhook).
- **AI proposals**: None.
- **Pricing**: Free. Freelancer Plus ($14.99/mo) gives marginally faster alerts + 100 Connects.
- **RSS feeds**: Discontinued August 2024. No longer an option.
- **Verdict**: Unusable for competitive $5K+ gigs. By the time you see the alert, 20+ proposals are already in.

### 2. GigRadar (Agency-focused)

- **Speed**: 2-5 minutes for job detection.
- **Filters**: 50+ job parameters via "scanners". Budget, client history, category, skills, payment verified, hire rate, spend history.
- **Integrations**: Slack, Telegram. CRM pipeline. Google Sheets export.
- **AI proposals**: Yes -- AI agent detects relevant jobs, generates personalized cover letters, and can auto-submit proposals within 15 minutes.
- **Scoring**: GigRadar Score (proprietary) ranks jobs based on your preferences + 50 parameters.
- **Team features**: Multi-seat plans, assignment workflow, pipeline tracking, funnel dashboard.
- **Webhook/API**: Supports webhook integrations and has a CRM with pipeline management. Hybrid model (webhooks for speed, polling for reliability).
- **Pricing**: Starts ~$49/mo. Agency plans higher (per-seat). Free trial available, no credit card required.
- **Compliance**: Uses Upwork data (not a browser extension that modifies pages). Agency-grade tool with established user base.
- **Best for**: Agencies wanting full automation -- alert to proposal to pipeline management.

### 3. Vollna (Budget-friendly)

- **Speed**: Near-instant alerts via Telegram/Slack/Discord.
- **Filters**: 30+ filter attributes. Budget, experience level, client rating, skills, category.
- **Integrations**: Slack, Discord, Telegram, Email. Google Sheets, Notion, CRM export.
- **AI proposals**: Yes -- generates and refines proposals directly in Telegram/Slack/Discord.
- **Auto-bidding**: Plans starting at $149/mo can auto-submit proposals.
- **Pricing**: Starts at $4/mo for basic filtering + alerts. Auto-bidding from $149/mo. 14-day free trial with all features.
- **Compliance**: Established tool, not a browser extension.
- **Best for**: Solo freelancers or small teams wanting affordable alerts + occasional AI proposals.

### 4. OutBid (Speed-focused)

- **Speed**: 60-second detection (fastest measured).
- **Filters**: Skills, budget, experience level. Quiet hours setting.
- **Integrations**: Telegram only.
- **AI proposals**: Yes -- AI-drafted proposal included with every alert, ready to copy-paste.
- **Pricing**: Free tier available. $9.99/mo for full access.
- **Compliance**: Telegram bot, no browser extension.
- **Best for**: Speed-first freelancers who want the fastest possible notification.

### 5. Other Notable Tools

| Tool | Speed | Price | Notes |
|------|-------|-------|-------|
| UpHunt | <30 seconds | Varies | Claims fastest alerts |
| Upwex | Real-time (browser open) | $5-20/mo | Chrome extension (compliance risk) |
| U Never Sleep | ~5 minutes | Varies | Uses official Upwork API (safest) |
| Convertix | Varies | Varies | Sales insights + AI automation |
| Pitch Pilot | Fast | Varies | Telegram bot + AI proposals |
| FreelanceFilter | Varies | Varies | Custom filtering |

### Comparison Matrix

| Criteria | Upwork Native | GigRadar | Vollna | OutBid |
|----------|--------------|----------|--------|--------|
| Alert speed | 30-60 min | 2-5 min | ~1-2 min | ~60 sec |
| Budget filter | Basic | Advanced (50+ params) | Advanced (30+ attrs) | Basic |
| Client quality filter | No | Yes (history, spend, rating) | Yes | No |
| Slack integration | No | Yes | Yes | No |
| Telegram | No | Yes | Yes | Yes |
| Webhook/API | No | Yes | Export only | No |
| AI proposals | No | Yes (auto-submit) | Yes (in-chat) | Yes (copy-paste) |
| Team/multi-seat | No | Yes | Limited | No |
| CRM/pipeline | No | Yes | Basic | No |
| Starting price | Free | ~$49/mo | $4/mo | Free/$9.99 |

---

## Part 2: Upwork Compliance Rules

**Critical**: Upwork actively detects and penalizes unauthorized automation. Key rules:

1. **Browser extensions** that read/modify Upwork pages can trigger warnings, restrictions, or bans.
2. **Approved API keys** are the only safe path for automation. You must apply and describe your use case.
3. **Tools that use Upwork's API** (like U Never Sleep) are the safest.
4. **Telegram/Slack bots** that monitor job feeds externally (not via browser) are generally safer.
5. **Auto-bidding** carries the most risk. Even with compliant tools, mass auto-proposals can flag your account.

**Recommendation for Ashlr.ai**: Use GigRadar or Vollna (established, agency-grade tools) for monitoring. Do NOT use browser extensions. Apply for an Upwork API key for any custom integrations.

---

## Part 3: Ideal Monitoring Setup for Ashlr.ai

### Search Filters (create 3-4 "scanners" / saved searches)

**Scanner 1: AI Development (Core)**
- Keywords: "AI development", "machine learning", "LLM", "GPT", "Claude", "AI agent", "AI automation", "generative AI"
- Budget: $5,000 - $50,000+ (fixed price) OR $75-200+/hr (hourly)
- Client filters: Payment verified, 4.5+ rating, $10K+ total spend, 80%+ hire rate
- Category: Web/Mobile/Software Development, AI/ML, Data Science
- Experience level: Expert

**Scanner 2: AI Consulting / Strategy**
- Keywords: "AI strategy", "AI consulting", "AI implementation", "AI integration", "AI roadmap", "AI transformation"
- Budget: $5,000 - $50,000+
- Client filters: Payment verified, 4.0+ rating, $5K+ total spend
- Category: IT & Networking, Business Consulting

**Scanner 3: Chatbot / Agent Development**
- Keywords: "chatbot", "AI assistant", "RAG", "conversational AI", "AI agent development", "workflow automation AI"
- Budget: $3,000 - $50,000+
- Client filters: Payment verified, 4.0+ rating
- Category: Web Development, AI/ML

**Scanner 4: High-Value Enterprise**
- Keywords: "enterprise AI", "AI platform", "MLOps", "AI infrastructure", "fine-tuning", "AI SaaS"
- Budget: $10,000 - $100,000+
- Client filters: Payment verified, 4.5+ rating, $50K+ total spend, enterprise badge preferred
- Category: Any

### No-Go List (auto-reject)
- Academic/research papers, homework
- Spec work / unpaid tests
- Unverified payment methods
- Budget under $3K (unless hourly at $100+/hr)
- "Looking for cheapest option" language
- Crypto/Web3 scam patterns
- Data labeling / annotation grunt work

### Notification Channel: Slack

**Why Slack over Telegram for Ashlr.ai:**
- Team collaboration (threaded discussions on each lead)
- Channel organization (#upwork-leads, #upwork-high-priority, #upwork-proposals)
- Integration ecosystem (connects to everything else)
- Professional context (already used for work)
- Webhook support for custom automations

**Channel structure:**
- `#upwork-hot-leads` -- High-scoring jobs ($10K+, strong client, perfect skill match). Alert immediately.
- `#upwork-leads` -- All qualified leads above threshold. Review during work hours.
- `#upwork-proposals` -- Drafted proposals ready for team review before submission.
- `#upwork-wins` -- Tracking submitted proposals, interviews, contracts won.

### Checking Cadence
- **Real-time alerts** via GigRadar/Vollna to Slack (24/7 monitoring)
- **Human review**: 3x daily (morning, midday, evening) -- spend 15 min reviewing and responding
- **Proposal submission**: Within 1-2 hours of job posting for high-priority leads
- **Weekly review**: Analyze win rates, adjust filters, refine scoring criteria

### Job Scoring Criteria (1-100 scale)

| Factor | Weight | Scoring |
|--------|--------|---------|
| Budget size | 25% | $5-10K = 50, $10-25K = 75, $25K+ = 100 |
| Client quality | 20% | Rating, spend history, hire rate, payment verified |
| Skill match | 20% | Keyword overlap with Ashlr.ai capabilities |
| Competition level | 15% | <5 proposals = 100, 5-15 = 60, 15-30 = 30, 30+ = 10 |
| Project clarity | 10% | Well-defined scope = 100, vague = 30 |
| Timeline fit | 10% | Reasonable timeline = 100, "need it yesterday" = 30 |

**Thresholds:**
- Score 80+: Immediate action. Drop everything and apply.
- Score 60-79: Review and decide within 2 hours.
- Score 40-59: Low priority. Apply if pipeline is thin.
- Score <40: Skip.

---

## Part 4: Recommended Tool Selection

### Primary: GigRadar (~$49/mo)

**Why GigRadar for Ashlr.ai:**
1. **Agency-first design** -- multi-seat, pipeline, team assignment
2. **50+ parameter scoring** -- the GigRadar Score automates initial qualification
3. **AI auto-proposals** -- generates and can auto-submit personalized cover letters
4. **Slack + Telegram** -- routes alerts to your preferred channel
5. **CRM pipeline** -- tracks leads from alert to proposal to contract
6. **Webhook support** -- enables custom Claude triage pipeline (see Part 5)

### Backup: Vollna ($4/mo for alerts)

Keep Vollna running as a secondary monitor at $4/mo. Different tools catch different jobs at different speeds. The $4/mo insurance is worth it.

### Optional: OutBid ($9.99/mo)

If you find GigRadar's 2-5 min delay too slow for the hottest leads, add OutBid's 60-second Telegram alerts as a speed layer. Use it as a "first responder" signal, then handle the full workflow in GigRadar/Slack.

**Total monthly cost: ~$55-65/mo** (GigRadar + Vollna backup)

---

## Part 5: Claude-Powered Triage Architecture

### Overview

```
[GigRadar/Vollna] --> [Webhook] --> [n8n/Zapier] --> [Claude API] --> [Slack]
     |                                                    |
     |                                                    v
     |                                            Score + Draft Proposal
     |                                                    |
     v                                                    v
  Raw job alert                                  #upwork-hot-leads (80+)
                                                 #upwork-leads (60-79)
                                                 #upwork-proposals (draft)
```

### Architecture Components

#### 1. Alert Ingestion (GigRadar Webhook --> n8n)

GigRadar supports webhook integrations. Set up an n8n instance (self-hosted or cloud) to receive job alerts:

```
Trigger: Webhook node (receives POST from GigRadar)
    |
    v
Parse: Extract job title, description, budget, client info, skills, URL
    |
    v
Deduplicate: Check MongoDB/Airtable for existing job ID
    |
    v
Forward to Claude for scoring
```

**Alternative**: Use the existing n8n workflow template "Automated Job Hunter: Upwork Opportunity Aggregator & AI-Powered Notifier" as a starting point. It already handles fetching, deduplication (MongoDB), and Slack routing.

#### 2. Claude Triage Agent (Scoring + Proposal Drafting)

Call the Claude API (Anthropic) via n8n's HTTP Request node:

**Scoring prompt:**

```
You are an AI consulting sales qualification agent for Ashlr.ai, an AI development
agency specializing in AI agents, LLM applications, RAG systems, chatbots, and
AI automation.

Evaluate this Upwork job posting and return a JSON score:

## Job Details
Title: {title}
Description: {description}
Budget: {budget}
Client Rating: {client_rating}
Client Spend History: {client_spend}
Client Hire Rate: {client_hire_rate}
Payment Verified: {payment_verified}
Proposals So Far: {proposal_count}
Skills Required: {skills}

## Scoring Criteria
- budget_score (0-100): $5-10K=50, $10-25K=75, $25K+=100. Under $5K=20.
- client_score (0-100): Based on rating, spend, hire rate, verification.
- skill_match (0-100): How well does this match AI dev/consulting capabilities?
- competition_score (0-100): <5 proposals=100, 5-15=60, 15-30=30, 30+=10.
- clarity_score (0-100): Is the project scope well-defined?
- red_flags (list): Any concerns (unrealistic timeline, spec work, scam indicators).

Return JSON:
{
  "total_score": <weighted average>,
  "budget_score": <int>,
  "client_score": <int>,
  "skill_match": <int>,
  "competition_score": <int>,
  "clarity_score": <int>,
  "red_flags": [],
  "one_line_summary": "<what this client actually needs>",
  "recommended_action": "apply_immediately | review_and_decide | skip",
  "proposal_angle": "<2 sentences on how to position Ashlr.ai for this gig>"
}
```

**Proposal drafting prompt (for score 60+):**

```
You are writing an Upwork proposal for Ashlr.ai, an AI development agency.

## Our Capabilities
- AI agent development (Claude Code, custom agents, orchestration)
- LLM application development (RAG, fine-tuning, prompt engineering)
- AI automation and workflow systems
- Chatbot and conversational AI
- AI strategy consulting
- Full-stack AI SaaS development

## Tone
- Confident but not arrogant
- Technical but accessible
- Focus on outcomes and ROI, not just technology
- Reference specific relevant experience
- Ask 1-2 smart clarifying questions to show domain understanding

## Job Details
{job_details}

## Scoring Analysis
{claude_scoring_output}

Write a proposal (200-300 words) that:
1. Opens with a specific insight about their problem (not "I read your job posting")
2. Demonstrates relevant expertise with a concrete example
3. Outlines a high-level approach (3-4 steps)
4. Includes a timeline estimate
5. Ends with 1-2 clarifying questions
6. Does NOT mention pricing (let them ask)
```

#### 3. Slack Routing (n8n --> Slack)

Based on Claude's scoring output, route to appropriate channels:

```
Score 80+  --> #upwork-hot-leads (with @channel notification)
              Message: Score, summary, proposal draft, "Apply Now" link

Score 60-79 --> #upwork-leads
              Message: Score, summary, brief analysis

Score <60  --> Log to Google Sheet only (no Slack noise)
```

**Slack message format:**

```
:dart: *New Lead: {title}* (Score: {total_score}/100)

*Budget:* {budget} | *Client:* {rating} stars, ${spend} spent
*Match:* {skill_match}/100 | *Competition:* {proposals} proposals

> {one_line_summary}

*Proposal angle:* {proposal_angle}

*Red flags:* {red_flags or "None"}

:page_facing_up: [Draft Proposal] (expandable)
:link: [View on Upwork]({url})

React :white_check_mark: to claim | :x: to skip
```

#### 4. Feedback Loop

Track outcomes in a Google Sheet or Airtable:

| Job ID | Score | Action Taken | Outcome | Notes |
|--------|-------|-------------|---------|-------|
| ... | 85 | Applied | Interview | Won $15K contract |
| ... | 72 | Applied | No response | Too many proposals already |
| ... | 45 | Skipped | -- | Budget too low |

Use this data monthly to refine:
- Scoring weights (is client_score more predictive than budget_score?)
- Filter keywords (which terms lead to wins?)
- Proposal templates (which angles get responses?)

### Implementation Steps

#### Week 1: Foundation
1. Sign up for GigRadar (free trial). Create 4 scanners per the filter specs above.
2. Sign up for Vollna ($4/mo). Create matching filters as backup.
3. Connect both to Slack via their native integrations.
4. Create Slack channels: `#upwork-hot-leads`, `#upwork-leads`, `#upwork-proposals`, `#upwork-wins`.
5. Set up Slack notification rules (hot-leads = all notifications, leads = during work hours only).

#### Week 2: Claude Triage Pipeline
1. Set up n8n (self-hosted on a $5/mo VPS, or n8n Cloud at $24/mo).
2. Create webhook endpoint in n8n.
3. Configure GigRadar to send webhook on new job match.
4. Build n8n workflow: Webhook --> Dedup --> Claude API (scoring) --> Claude API (proposal) --> Slack routing.
5. Test with 10-20 real job alerts. Calibrate scoring thresholds.

#### Week 3: Optimization
1. Review first week's scores vs. your gut reaction. Adjust weights.
2. Refine proposal templates based on what gets responses.
3. Add Google Sheets logging for outcome tracking.
4. Consider adding OutBid ($9.99/mo) if speed matters for your niche.

#### Week 4: Steady State
1. Daily routine: Check `#upwork-hot-leads` 3x/day, review Claude's proposals, submit (with human editing) within 1-2 hours.
2. Weekly: Review win/loss data, tune filters and scoring.
3. Monthly: Analyze ROI (tool costs vs. contract revenue), adjust scanner keywords.

### Cost Estimate

| Item | Monthly Cost |
|------|-------------|
| GigRadar | ~$49 |
| Vollna (backup alerts) | $4 |
| n8n Cloud | $24 (or $5 self-hosted) |
| Claude API (scoring + proposals) | ~$5-15 (est. 500 jobs/mo) |
| **Total** | **~$82-92/mo** |

At a target of $5K-$50K contracts, winning even one contract per quarter pays for 3+ years of tooling.

---

## Part 6: Could a Claude Code Agent Handle Triage?

Yes, but the n8n/API approach above is better for this use case. Here's why:

**Claude Code agent approach:**
- Would require a running agent watching a webhook endpoint or polling Slack
- Overhead of maintaining a persistent process
- Better suited for complex multi-step coding tasks, not event-driven alert processing

**Claude API via n8n approach (recommended):**
- Event-driven (only runs when a job alert arrives)
- Stateless (no process to maintain)
- Cheaper (API calls vs. persistent agent session)
- Easier to monitor and debug (n8n has visual workflow editor + execution logs)
- Can be enhanced later with Airtable CRM, auto-proposal submission, etc.

**However**, a Claude Code agent COULD be useful for a different part of the pipeline: **deep research on high-scoring leads**. For any job scoring 85+, you could trigger a Claude Code agent to:
1. Research the client's company
2. Look at their past Upwork history
3. Analyze competitors who might bid
4. Draft a deeply personalized proposal with specific technical recommendations
5. Prepare talking points for the interview

This would be a "premium triage" step for only the hottest leads, not every alert.

---

## Quick-Start Checklist

- [ ] Create Upwork agency profile for Ashlr.ai (if not done)
- [ ] Sign up for GigRadar free trial
- [ ] Create 4 scanners (AI Dev, AI Consulting, Chatbot/Agent, Enterprise)
- [ ] Sign up for Vollna ($4/mo backup)
- [ ] Create Slack channels (#upwork-hot-leads, #upwork-leads, #upwork-proposals, #upwork-wins)
- [ ] Connect GigRadar + Vollna to Slack
- [ ] Apply for Upwork API key (for future custom integrations)
- [ ] Set up n8n instance
- [ ] Build Claude scoring + proposal workflow in n8n
- [ ] Test with 20 real alerts, calibrate thresholds
- [ ] Establish daily review cadence (3x/day, 15 min each)
- [ ] Track outcomes in Google Sheets for monthly optimization

---

## Sources

- [GigRadar - AI Upwork Automation for Agencies](https://gigradar.io/)
- [GigRadar Real-Time Job Alerts Guide](https://gigradar.io/blog/real-time-upwork-job-alerts)
- [GigRadar for Teams](https://gigradar.io/blog/gigradar-for-teams)
- [Vollna - Upwork Auto Bidding & AI Proposals](https://www.vollna.com/)
- [Vollna Pricing](https://www.vollna.com/pricing)
- [Vollna vs Upwork Job Alerts](https://www.vollna.com/blog/vollna-vs-upwork-job-alerts)
- [Vollna - Generate Proposals in Telegram/Slack/Discord](https://www.vollna.com/blog/generate-and-refine-upwork-proposals-directly-in-telegram-slack-and-discord)
- [OutBid - Upwork Job Alerts in 60 Seconds](https://useoutbid.com/)
- [OutBid - Every Upwork Alert Option Compared (2026)](https://useoutbid.com/blog/upwork-job-alerts-every-option-compared)
- [GigRadar vs GetMany vs UpHunt Comparison (2026)](https://uphunt.io/blog/gigradar-vs-getmany-vs-uphunt)
- [5 Best Upwork Automation Tools for Agencies (2026)](https://convertix.io/blog/5-best-upwork-automation-tools-for-agencies-in-2026/)
- [Upwork - Use Bots and Automation Properly](https://support.upwork.com/hc/en-us/articles/43342677368467-Use-bots-and-other-automation-properly)
- [Upwork - How to Request an API Key](https://support.upwork.com/hc/en-us/articles/115015857647-Request-an-API-Key)
- [n8n - Automated Upwork Job Hunter Workflow](https://n8n.io/workflows/4733-automated-job-hunter-upwork-opportunity-aggregator-and-ai-powered-notifier/)
- [n8n - Upwork Alerts with MongoDB & Slack](https://n8n.io/workflows/2834-automated-upwork-job-alerts-with-mongodb-and-slack/)
- [Vollna Pricing & Features (2026) - SoftwareSuggest](https://www.softwaresuggest.com/vollna)
- [GigRadar Reviews (2026) - SoftwareWorld](https://www.softwareworld.co/software/gigradar-reviews/)
