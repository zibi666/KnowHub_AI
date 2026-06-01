# Custom Base URL API Keys Implementation Plan

> **For AI agents:** Use this as the execution checklist for adding per-user BaseURL profiles with separate chat and image API keys. Keep changes small, run targeted tests after each backend stage, and push `design/yzy` to both configured remotes when complete.

**Goal:** Let each user save multiple complete BaseURL profiles, activate exactly one profile at a time, and configure one chat API key plus one image API key per profile.

**Architecture:** Add a `user_model_endpoints` table for per-user BaseURL profiles and link existing `user_api_key_entries` to an endpoint. Continue using the current `gpt-chat` and `gpt-image` key groups for routing. Model discovery happens per endpoint and purpose using the user-provided full BaseURL.

**Tech Stack:** FastAPI, SQLAlchemy async, SQLite/MySQL startup migrations, Vue 3, existing API key services and model settings routes.

---

### Task 1: Backend Tests

**Files:**
- Create: `backend/tests/test_base_url_profiles.py`
- Modify only if needed: `backend/tests/conftest.py`

- [ ] Add tests for default endpoint creation from `settings.model_base_url`.
- [ ] Add tests for creating chat and image keys under the active endpoint with provider probing called using that endpoint BaseURL.
- [ ] Add tests for model aggregation only from the active endpoint.
- [ ] Add tests for missing image key when the selected model is an image model.

### Task 2: Data Model And Migration

**Files:**
- Modify: `backend/app/models/entities.py`
- Modify: `backend/app/core/db.py`
- Modify: `backend/app/services/api_keys.py`

- [ ] Add `UserModelEndpoint` with `user_id`, `name`, `base_url`, `is_active`, `status`, `last_probe_error`, and timestamps.
- [ ] Add nullable `endpoint_id` and `base_url` compatibility column to `UserApiKey`.
- [ ] Add startup migrations for SQLite/MySQL.
- [ ] Ensure old users and old keys get a default endpoint using `settings.model_base_url`.

### Task 3: API Surface

**Files:**
- Modify: `backend/app/schemas/api_keys.py`
- Modify: `backend/app/api/routes/api_keys.py`
- Modify: `backend/app/services/api_keys.py`

- [ ] Add endpoint profile list/create/update/delete/activate routes.
- [ ] Extend key create/update response payloads with `endpoint_id`, `base_url`, and full secret exposure for frontend copying.
- [ ] Ensure key creation tests connectivity immediately against the selected endpoint BaseURL.
- [ ] Return detailed validation/probe errors without hiding the HTTP status or upstream message.

### Task 4: Runtime Routing

**Files:**
- Modify: `backend/app/services/api_keys.py`
- Modify: `backend/app/services/chat.py`
- Modify: `backend/app/services/image_generation.py`
- Modify: `backend/app/services/attachments.py`
- Modify: `backend/app/services/compaction.py`
- Modify: `backend/app/services/context.py`

- [ ] Resolve chat keys only from the active endpoint and chat group.
- [ ] Resolve image keys only from the active endpoint and image group.
- [ ] Pass the selected key BaseURL into OpenAI-compatible chat/embedding providers and image generation HTTP calls.
- [ ] Raise specific missing-key errors for active endpoint chat/image gaps.

### Task 5: Frontend

**Files:**
- Modify: `frontend/src/types/index.ts`
- Modify: `frontend/src/api/client.ts` if needed
- Modify: `frontend/src/views/ChatView.vue`
- Modify: `frontend/src/views/KeyManagementView.vue`
- Modify: `frontend/src/views/AdminView.vue` if admin key management remains visible there

- [ ] Add BaseURL profile controls to settings/key management.
- [ ] Show active profile and allow manual switching.
- [ ] Show chat key and image key sections under the active BaseURL.
- [ ] Reload model options after switching profiles or saving keys.
- [ ] Show clear missing chat/image key messages.

### Task 6: Verification And Git

**Files:**
- Tests and build output only.

- [ ] Run targeted backend tests.
- [ ] Run frontend type/build check.
- [ ] Inspect `git diff`.
- [ ] Commit with a clear message.
- [ ] Push `design/yzy` to GitHub and Gitee through `origin`.
