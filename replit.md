# InboxPilot v2.0

## Overview

InboxPilot is a FastAPI-based email assistant designed to centralize email management across Microsoft Outlook, Apple Mail, and Gmail. It fetches unread emails, generates AI-powered reply suggestions, and facilitates email actions like marking as read and deleting. The project aims to deliver intelligent email automation, a unified inbox, a web-based dashboard, and conversational AI features including a "Hands Free" voice-controlled mode. Key capabilities include session-based email collection, batch action execution, PDF/TXT exports, and LLM-driven email triage, chat, and action dispatch.

## User Preferences

Preferred communication style: Simple, everyday language.

## System Architecture

InboxPilot employs a pluggable provider layer architecture, abstracting email service integrations through a common `EmailProvider` interface. The core application is built with FastAPI, emphasizing asynchronous operations and automatic API documentation.

**Key Architectural Patterns:**

-   **Provider Abstraction:** An `EmailProvider` Abstract Base Class enables modular and extensible integration with various email services (Apple Mail, Microsoft Outlook, Gmail).
-   **Modular Structure:** The codebase is organized into distinct modules for concerns such as LLM integration, HTTP wrappers, and utilities.
-   **Environment-based Configuration:** All configurations and sensitive data are managed via environment variables.
-   **Offline-First Architecture:** Utilizes IndexedDB for client-side offline storage, managing command and cognitive queues, and providing offline data access.
-   **Background Job Queue:** Implements a persistent SQLite-based job queue with a background worker for resilient and rate-limited LLM processing.

**UI/UX Decisions:**

-   A web-based dashboard at `/dashboard` (and `/ui`) provides a unified interface for email management, queue management, and AI interaction (chat, triage, reply suggestions). It is built with vanilla HTML/CSS/JS.
-   The dashboard supports both direct user interaction and JSON import/export for advanced workflows.
-   "Hands Free" mode offers voice-controlled email management via OpenAI's Speech-to-Text and Text-to-Speech, with three modes: Manual (tap mic), Auto (auto-listen after TTS), and Always-On (continuous hotword "Rordens" detection via Web Speech API with anti-feedback state machine). Configurable speaking speed, voice selection, and language normalization.
-   **Provider-Aware Filtering:** HandsFree "Fonte" selector lets users filter by Dashboard/Apple/Gmail; provider filtering is applied at snapshot, chat, and dispatch levels with context_id validation.

**Technical Implementations:**

-   **Backend Framework:** FastAPI (Python) for robust RESTful APIs.
-   **Email Integrations:**
    -   **Microsoft Outlook:** Uses Microsoft Graph API v1.0 with OAuth 2.0 (MSAL).
    -   **Apple Mail:** Integrates via IMAP for reading and SMTP for sending (app-specific passwords).
    -   **Gmail:** Uses Gmail API v1 with OAuth 2.0 (`google-auth-oauthlib`).
-   **Unified Endpoints:** Provider-agnostic endpoints (`/unified/*`) ensure consistent interaction across email providers.
-   **Automation Engine:** Configurable automation (`/automation/*`) based on `policy.json` supports rule-based email processing with dry-run and auditing.
-   **Unified Inbox API:** Aggregates emails from all providers with advanced date range filtering (`/inbox/*`, `/ui/messages`).
-   **Queue System:** Manages batch email actions (`/queue/*`) and LLM-generated actions (`/llm/queue/*`).
-   **Export Endpoints:** Provides PDF and TXT exports for emails (`/export/pdf`) and supports dispatching LLM-generated action plans (`/dispatch/import`).
-   **AI Integration (LLM Cognitive Layer):**
    -   Leverages OpenAI's GPT models for reply suggestions (`/llm/suggest-reply`), email triage (`/llm/triage`), and conversational assistance (`/llm/chat`).
    -   Includes blocking rules for common email types (OTP, no-reply, newsletters).
    -   Features LLM response caching in SQLite and audit logging for all LLM calls.
    -   Supports per-action model selection (e.g., `gpt-4.1-mini` for triage, `gpt-4.1` for replies).
    -   Integrates with a persistent snapshot system for caching email contexts used by LLMs.

**Data Storage:**

-   OAuth tokens for providers are stored in local JSON files or SQLite (e.g., Gmail tokens).
-   Email drafts are persisted using a file-based key-value store.
-   Automation audit logs, LLM cache, conversations, and LLM job queues are stored in SQLite databases.
-   Client-side data caching and queuing handled by IndexedDB.

## External Dependencies

**APIs and Services:**

-   **Microsoft Graph API:** For Microsoft Outlook email services.
-   **OpenAI API:** For AI-powered email analysis, reply generation, speech-to-text, and text-to-speech.
-   **Apple iCloud IMAP/SMTP:** Protocols for Apple Mail operations.
-   **Gmail API:** For managing Gmail accounts.

**Python Packages:**

-   `fastapi`: Web framework.
-   `uvicorn`: ASGI server.
-   `msal`: Microsoft Authentication Library.
-   `requests`: HTTP client.
-   `python-dotenv`: Environment variable management.
-   `google-auth`, `google-auth-oauthlib`, `google-api-python-client`: Gmail OAuth and API.
-   `imaplib`, `smtplib`: Standard Python IMAP/SMTP libraries.
-   `reportlab`: PDF generation.