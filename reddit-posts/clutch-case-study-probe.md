# Clutch.co Case Study: Probe AI

## Project Summary
**Project Name:** Probe AI — Multi-Agent Deep Research Platform
**Client:** Internal product (AshlrAI)
**Industry:** Artificial Intelligence, SaaS, Research Technology
**Project Type:** Full-Stack AI SaaS Development
**Timeline:** 3 months (concept to production launch)
**Team Size:** 1 (solo developer/architect)
**Budget Range:** $50,000-$199,999 equivalent in development time
**Live URL:** tryprobe.io

## The Challenge
Traditional research tools require users to manually search multiple sources, cross-reference information, and synthesize findings. We needed to build an AI-native platform that could deploy multiple AI agents in parallel to search the web and X/Twitter simultaneously, then synthesize comprehensive research reports in real-time — with a production-grade SaaS wrapper including billing, team management, and automation.

## Our Approach

### Architecture
- **Frontend:** Next.js 15 (App Router), React, Tailwind CSS v4, TypeScript
- **Backend:** Supabase (Auth + PostgreSQL + Row-Level Security), Vercel Pro (300s timeout)
- **AI:** xAI API via OpenAI SDK for multi-agent orchestration
- **Billing:** Stripe Checkout (live) with usage-based credit system and tiered top-up bonuses
- **Real-time:** Server-Sent Events (SSE) for streaming research output
- **Monitoring:** Sentry, Upstash rate limiting

### Key Technical Decisions
1. **SSE streaming** over WebSocket for real-time output — simpler, more reliable for one-directional data flow
2. **Optimistic billing** — deduct balance before research, auto-refund on failure — prevents abuse while maintaining UX
3. **Row-Level Security** across 20+ tables — every query is scoped to the authenticated user or organization
4. **Agent simulation grid** — 16-agent visualization showing parallel research progress in real-time

## What We Built

### Core Features
- Multi-agent research with 16 parallel AI agents searching web + X/Twitter
- Real-time streaming output with agent visualization
- Usage-based credit billing ($0.15-$5 per query depth)
- Collections — project folders with custom context prompts
- Scheduled research automations (recurring/one-time, cron-based)
- Semantic memory — auto-extracts user interests for personalized queries
- Team/org management with role-based access and shared billing
- Research export (PDF, DOCX, XLSX, HTML)
- Referral system, bookmarks, command palette, mobile-responsive UI

### Infrastructure
- 20+ database tables with RLS policies
- Automated signup bonus, profile creation via database triggers
- Stripe webhook handling for payment events
- Cron-based content engine (blog generation, social posting, sports picks)
- Sentry error tracking, Upstash rate limiting

## Results
- **Launched on Product Hunt** (March 2026)
- **Live with paying users** generating revenue
- **102 views, 2 watchers** on product marketplace listing
- **231 passing tests** across 12 test files
- **Clean production build** with zero critical vulnerabilities
- **4 organizations** using the platform including team billing

## Technologies Used
Next.js 15, TypeScript, React, Tailwind CSS v4, Supabase, PostgreSQL, Stripe, Vercel, Sentry, Upstash Redis, xAI API, SSE Streaming, Zod, SWR, Recharts, jsPDF

## Key Takeaway
This project demonstrates end-to-end product development — from architecture and database design through billing, team management, and production deployment. Every component was designed for production from day one, not retrofitted from a prototype.
