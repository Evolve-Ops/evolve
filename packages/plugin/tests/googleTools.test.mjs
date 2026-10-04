/**
 * googleTools — pins the Calendar write surface (brief
 * google-calendar-delete-and-update-events, D-AS2).
 *
 * A bot that can create a calendar event must be able to move and retract
 * it, and the tools must tell the model their own rules up front (ownership,
 * confirm) instead of letting it discover them by failing. These tests pin
 * the schema shape, the gate class, and the profile membership so a later
 * edit cannot quietly drop one of the three.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import { TOOL_DEFS } from "../dist/tools/GoogleTools.js";
import { requiredCapabilityFor } from "../dist/integrity/ToolCallGate.js";

const byName = Object.fromEntries(TOOL_DEFS.map((d) => [d.name, d]));

test("the Calendar surface is list / create / update / delete", () => {
  const cal = TOOL_DEFS.map((d) => d.name).filter((n) => n.startsWith("calendar_"));
  assert.deepEqual(cal.sort(), [
    "calendar_create_event",
    "calendar_delete_event",
    "calendar_list_events",
    "calendar_update_event",
  ]);
});

test("calendar_delete_event requires event_id and confirm; force/scope optional", () => {
  const p = byName.calendar_delete_event.params;
  assert.deepEqual([...p.required].sort(), ["confirm", "event_id"]);
  for (const k of ["calendar_id", "force", "scope"]) {
    assert.ok(k in p.properties, `missing ${k}`);
  }
  assert.equal(p.properties.confirm.type, "boolean");
  const d = byName.calendar_delete_event.description;
  assert.match(d, /confirm: true/);
  assert.match(d, /you created/);
});

test("calendar_update_event is PATCH-shaped: only event_id is required", () => {
  const p = byName.calendar_update_event.params;
  assert.deepEqual([...p.required], ["event_id"]);
  for (const k of ["summary", "start", "end", "description", "location",
                   "attendees", "calendar_id", "scope", "force",
                   "send_updates", "start_weekday"]) {
    assert.ok(k in p.properties, `missing ${k}`);
  }
  assert.ok("replace_attendees" in p.properties, "missing replace_attendees");
  assert.match(p.properties.attendees.description, /adds guests.*replace_attendees/);
  assert.match(p.properties.replace_attendees.description, /dropped guests are emailed/);
  assert.match(p.properties.start.description, /ISO 8601/);
  assert.match(p.properties.end.description, /ISO 8601/);
  // The move-not-duplicate rule lives in the schema the model reads.
  assert.match(byName.calendar_update_event.description, /MOVE/);
  assert.match(byName.calendar_update_event.description, /never create/);
});

test("calendar_create_event carries the truthfulness params", () => {
  const p = byName.calendar_create_event.params;
  for (const k of ["start_weekday", "recurrence", "send_updates", "timezone"]) {
    assert.ok(k in p.properties, `missing ${k}`);
  }
  assert.match(byName.calendar_create_event.description, /RETURNED/);
  assert.match(byName.calendar_create_event.description, /start_weekday/);
});

test("every Calendar write is gated as bot.send_external; list stays safe", () => {
  for (const n of ["calendar_create_event", "calendar_update_event",
                   "calendar_delete_event"]) {
    assert.equal(requiredCapabilityFor(n), "bot.send_external", n);
  }
  assert.equal(requiredCapabilityFor("calendar_list_events"), undefined);
});
