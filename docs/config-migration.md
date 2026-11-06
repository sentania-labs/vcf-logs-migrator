# Ops for Logs config migration: 8.18.7 to 9.0.2.0200

Research run 2026-11-06, 8:00 to 8:25 AM CST. Source 8.18.7-25423541 at <8.18 primary> (a cluster that grew to 3 nodes during the run). Destination 9.0.2.0200.25430789 at <9.0 node> (it restarted at least twice during the run, not me). All calls on :9543 with a Bearer session from `POST /api/v1/sessions`. I did not restart any service and did not create a partition, so no restart was needed.

## Short version

- The REST surface for config is almost the same on both versions. Every config GET I tried returns the same JSON shape on 8.18 and 9.0. In most cases you can POST the 8.18 GET output to 9.0 unchanged.
- Things that carry over 1:1 by REST: log masking rules, log filters, partitions (same name gives the same id), agent groups, alerts, webhooks, datasets, extracted fields, dashboards and other user content, and content packs.
- Things that need edits: forwarders (SSL targets need `acceptCert:true`, which also adds a trusted cert), roles (capabilities have to be flattened to strings and dataset ids remapped), and alerts that use webhooks (webhook ids need remapping).
- No REST for: SMTP server and query forwarder (both live only in the config XML or the export bundle), and the VCF Ops integration. 9.0 dropped `/api/v*/vrops`, because the integration is now driven from the VCF Ops side.
- The built-in "export/import configuration" API (`/api/v1/config/data`) exists on both versions. Its upload step is broken on both: it returns "Successfully uploaded" but the file never lands, so the apply step fails. If you copy the tarball to the path it expects over SSH, the apply works on 9.0.
- 9.0 has a built-in log data transfer (`/api/v2/data-migrations`, root SSH to the source, copies buckets). It only reads the source's DistributedConfig (the node list). It does not carry config.

## Config area table

"API" = method + path that exists. "Shape change" = 8.18 GET vs 9.0 GET JSON structure.

| Config area | 8.18 API | 9.0 API | Shape change | Carried by API? | Carried by content pack? | Manual only | Notes |
|---|---|---|---|---|---|---|---|
| Event forwarding destinations | GET/POST/PUT/PATCH/DELETE `/api/v{1,2}/log-forwarder`, `/batch`, `/testconnection` | same, plus `PATCH /api/v2/log-forwarder/reset/stats` and 9.0-only `/api/v2/internal/log-forwarder-credentials` | none | **Yes, with edits** | no | no | GET omits `acceptCert`. An SSL target whose cert is not trusted fails with 495 even with `testConnection:false`, so add `acceptCert:true`. That auto-adds a `FORWARDER_<host>` entry to trusted-certificates. Id = name-derived UUID, same on both sides. Filter syntax is the same (`appname="x"`). 9.0 refuses a forwarder pointing at itself. Batch body is `{"forwarders":[...]}`, but GET returns a bare list, so wrap it. |
| Log masking rules | `/api/v{1,2}/log-mask`, `/batch` | same | none | **Yes, 1:1** | no | no | GET body POSTs unchanged. `POST /log-mask/batch` takes the GET wrapper `{"logMaskRules":[...]}` directly. Same name gives the same id. The selector needs a capture group. Masking is applied before forwarding (proven). |
| Log filters (drop rules) | `/api/v{1,2}/log-filter`, `/batch` | same | none | **Yes, 1:1** | no | no | Same name gives the same id. |
| Index partitions + per-partition retention + archive | `/api/v{1,2}/partitioning`, `/test-archive`, `/status/:id` | same | none | **Yes, 1:1** | no | restart | `retentionPeriodSeconds`, `archiveEnabled` and `archiveLocation` are per partition. Id = `UUID.nameUUIDFromBytes(name)`. A new partition needs `systemctl restart loginsight` before it routes (known; not re-tested). |
| Extracted fields | content API only: `/api/v1/content/{user,shared}content` (import), `.../extractedfield` (list/delete) | same, **plus new** `/api/v2/fields/extractedFields` (POST, GET/PATCH/DELETE by internalName) | none on the content API | **Yes, via user/shared content import** | yes | no | `internalName` comes from the name and is the same on both sides (`cfgmig_user` gave `ibadax3dmztw22lhl52xgzls` on both), so saved queries that reference it keep working. The 9.0 v2 field API has a flattened shape (`extractionRegex`, `filterFieldName`...) and has no 8.18 equivalent. |
| User dashboards, bookmarked queries | `GET /api/v1/content/usercontent?namespace=X` exports the caller's content as a content pack; `POST` imports | same | none | **Yes, 1:1** (per user, as that user) | it *is* a content pack | no | Dashboard id is name-derived (same on both). Re-import is idempotent. The export runs as the logged-in user, so other users' content needs their session. |
| Shared content | `/api/v1/content/sharedcontent` (GET needs `?namespace=`) | same | none | Yes (same format, not exercised) | yes | no | |
| Content packs (installed) | `GET /api/v1/content/contentpack/:ns` (full pack JSON), `POST /api/v1/content/contentpack` (`?overwrite`) | same | none | **Yes, 1:1** | n/a | no | Proven with com.vmware.vrops67 (50 fields, 9 alerts, dashboards). 9.0 ships its own general/vsphere/vsan 9.0 packs plus `com.vmware.VCFOperationsForLogs`. Do not push the 8.18 vSphere 8.18 pack over the 9.0 one. Only move the extras. |
| Agent groups (incl. agent config / liagent.ini) | `/api/v{1,2}/agent/groups` POST/PUT/DELETE | same | none | **Yes, 1:1** | partial | no | POST takes the GET item as is (`name`, `criteria`, `agentConfig`, `info`). Id is random, and delete is by name. The user-content export contains `agentClasses`, but **without `criteria`**, so use the agent groups API, not content. Also carried by the config export bundle. |
| Individual agent state/config | `/api/v1/agent`, `/agent/config/:id` | same | none | n/a | no | n/a | Agents re-register against the new target. Nothing to migrate. |
| Alerts (user alerts) | `/api/v1/alerts` (flat), `/api/v2/alerts` (nested `query`/`recipients`) | same, v2 adds `integrationType` | v2 adds `integrationType` | **Yes, 1:1 for v1 shape** | yes (content pack alerts) | no | Raw 8.18 v1 GET POSTs to 9.0 fine. You get a new id, and owner becomes the caller. `webhookIds` must be remapped to the new webhook ids. Alerts were *not* in the user-content export. |
| Alerts to VCF Ops (9.0) | n/a (8.18 used `vcopsEnabled` on alerts + `/vrops`) | **new** `/api/v2/ops-alerts` (+ batch) | new | n/a | no | rebuild | 9.0-only. 8.18 vROps-alert fields (`vcopsEnabled`, `vcopsResourceKindKey`) are still in the alert shape. |
| Webhooks | `/api/v{1,2}/notification/webhook` | same, plus `POST /api/v2/notification/webhook/test-ui` | none | **Yes, 1:1** | no | no | New id on the destination. The URL may not be local/loopback. `acceptCert:true` on create was stored as false. |
| System notification recipients | `/api/v1/notification/email`, `/notification/channels`, `/notifications` | same | none | Yes (PUT) | no | no | Also in the export bundle as "general". |
| Retention notification threshold | `/api/v1/notification/config/retention-threshold` | same | none | Yes (PUT) | no | no | |
| SMTP server | **no REST endpoint** | **no REST endpoint** | n/a | only via the config export bundle (`smtpConfig`) | no | UI otherwise | The bundle carries the SMTP password in clear text, so treat the file as a secret. |
| NTP / time | `/api/v1/time/config` (PUT), `/time/test` | same | none | Yes | no | no | Both lab nodes use `ESX_HOST`. |
| CEIP | `/api/v1/ceip` | same | none | Yes | no | no | |
| Users (local) | `/api/v1/users` | same | none | create yes, password no | no | passwords | Passwords are not exportable. Recreate with new passwords. |
| User groups (AD/vIDM/vIDB) | `/api/v1/user-groups` | same | none | Yes (`provider`, `domain`, `name`, `roleIds`) | no | after auth provider | Needs the auth provider configured first, plus remapped `roleIds`. |
| Roles | `/api/v1/roles`, `/:id/capabilities`, `/:id/datasets`, `/:id/users` | same | GET returns `capabilities:[{"id":..}]`, but POST wants `["..."]` (same on both versions) | **Yes, with edits** | no | no | Raw GET POST gives a 400 JSON_FORMAT_ERROR. Flatten capabilities and set `dataSets` to the new dataset ids. Built-in Super Admin/User keep fixed ids. View Only Admin and Dashboard User ids differ per install. 9.0 adds capabilities `{VIEW,EDIT}_LOG_CONFIG`, `{VIEW,EDIT}_LOG_DATA_TRANSFERS`, `{VIEW,EDIT}_SERVICE_ACCOUNT_CREDENTIALS`. |
| Datasets (RBAC data filters) | `/api/v1/datasets` | same | none | **Yes, 1:1** | no | no | You get a new id, so remap it into roles. |
| AD auth | `/api/v1/ad` (+ `/test`) | same | none (both disabled here) | GET has no bind password | no | **bind password** | Out of scope to change. Re-enter the bind credential. |
| vIDM / VCF Identity Broker | `/api/v1/vidm`, `/api/v2/vidb` | same | none (both disabled) | config yes, secret no | no | client secret | 9.0 adds `oauth-client-secret-ttl` in its auth config. |
| vSphere integration | `/api/v1/vsphere/:vcHostname`, `/api/v2/vsphere` | adds `/api/v2/vsphere/:UUID...`, `POST /api/v2/vsphere/hosts`, `PATCH .../hosts/batch` | none at list level (both empty) | config yes, password no | no | **vCenter password** | Not in the export bundle, by design. |
| VCF Ops / vROps integration | `/api/v{1,2}/vrops` (+ testconnection) | **removed** | removed | no | no | **yes, from VCF Ops** | 9.0 has `/api/v2/capabilities` (VCF capability registration, 404 until registered) and a `vcf-capabilities` config section instead. |
| vROps packs | `/api/v1/vropspacks` | same | none | Yes | n/a | no | |
| NSX integration | `/api/v2/nsx`, `/nsx/providers` | same | none (both 204) | config yes, secret no | no | credentials | |
| Web proxy | `/api/v2/proxies` | same | none | Yes | no | proxy password | |
| Limits | `/api/v2/limits` | same | none | Yes (PATCH) | no | no | Only `max-log-forwarder-count=10` on both. |
| Certificates (appliance SSL) | `/api/v1/certificate` | same, plus `GET /api/v2/certificate-management/certificate-bundle` | n/a | no (not touched) | no | **yes** | Not exercised, on purpose. |
| Trusted certificates | `/api/v2/trusted-certificates` (+ `/host`) | same | none | Yes | no | no | Gets touched as a side effect of `acceptCert:true`. |
| License | `/api/v1/licenses` GET/POST/DELETE | GET only (POST/DELETE removed), different shape | **yes** | no | no | **yes** | 9.0 licensing comes from VCF. |
| Usage reports | `GET /api/v2/usage-reports` | removed | removed | n/a | n/a | n/a | |
| CFAPI/syslog ingestion | ingest `/api/v{1,2}/events/ingest/:agentId` on 9543 (unauthenticated) | same | none | n/a | no | n/a | Port 9000 answers 403 "Only SSL connections are allowed" on both. Unauthenticated ingest on 9543 works on both. Listener ports are not REST-configurable. |
| Query forwarder | no REST | no REST | n/a | no | no | **yes (UI/XML)** | `QueryForwarderConfig` exists in the config model only. |
| Log export / scheduled reports | `/api/v1/log-export`, `/reports` | same | not compared (empty) | not tested | no | | |
| Shared dashboard URLs | `/api/v1/shared-dashboard-url` | same | not compared | not tested | no | | |
| Whole-config bulk export | `GET /api/v1/config/data` (gzip tar with `config_data.json`), `POST` same path | same | same format | **Upload broken on both; apply works if the file is placed by hand** | n/a | n/a | Covers NTP, forwarders, SMTP, archiving, agent groups, and general settings (CEIP, alerts config, session timeout). |
| Raw cluster config XML | `GET/PUT /api/v1/config` | same | 9.0 adds `vcf-capabilities` and `ndc` sections; 8.18 has `repository` and `ntp` sections | do not use | n/a | n/a | It contains the cluster encryption key and daemon token. Don't PUT one cluster's XML onto another. |
| 9.0-only: log collection configs | n/a | `/api/v2/log-configs` (+ `/status`, `/li-destinations`, `PATCH /config/:category`) | new | n/a | n/a | n/a | VCF component log-config management. Nothing to bring from 8.18. |
| 9.0-only: data transfer | n/a | `/api/v2/data-migrations` (+ `/test`) | new | data only | n/a | n/a | Body `{hostName, username, password, bucketStartTimeMillis, bucketEndTimeMillis}`. It SSHes to the source, runs `bucket-index show`, and copies inactive buckets. |

## Route table diff

Source: `unzip -p /usr/lib/loginsight/application/lib/api-play-service_2.13-1.0.jar routes` (same jar name on both). Unique method+path pairs: 8.18 = 672, 9.0 = 698. Of these, 17 were removed and 43 added. Only one same-path handler changed signature: `GET /api/v2/vcfadmin/vcevents/inventory` replaced `categorizedEventsOnly` with `eventType`.

**Removed in 9.0 (17):**
- vROps integration: `GET/POST/PUT/PATCH/DELETE /api/v{1,2}/vrops`, `POST /api/v{1,2}/vrops/testconnection` (12)
- Licensing writes: `POST /api/v{1,2}/licenses`, `DELETE /api/v{1,2}/licenses/:licenseId` (4)
- `GET /api/v2/usage-reports` (1)

**Added in 9.0 (43), by area:**
- Extracted fields v2: `POST /api/v2/fields/extractedFields`, `GET/PATCH/DELETE .../:internalName`
- Ops alerts: `GET/POST /api/v2/ops-alerts`, `GET/PATCH/DELETE .../:alertId`, `POST batch-create`, `PATCH batch-update`, `DELETE batch-delete`
- Log collection configs: `GET /api/v2/log-configs`, `GET/PATCH .../config/:category`, `GET .../li-destinations`, `GET .../status`, `POST .../:collector`, `POST .../status/:collector`
- Data transfer: `GET/POST/DELETE /api/v2/data-migrations`, `POST .../test`
- VCF capabilities: `GET/POST/PUT/DELETE /api/v2/capabilities`, `GET/PUT/DELETE .../:id`
- Forwarder credentials (internal): `GET/DELETE /api/v2/internal/log-forwarder-credentials/:id`, `PUT/PATCH .../log-forwarder-credentials`
- Certificates: `GET /api/v2/certificate-management/certificate-bundle`
- Search/diagnostics: `POST /api/v2/search`, `POST /api/v2/search/compare`, `GET /api/v2/search/compare/:routing_token`, `POST /api/v2/diagnostics/{extractedField,multiline,timerange}`
- VC events charts: `GET /api/v2/vcfadmin/vcevents/charts/{perms,session}`

Everything else config-related (forwarder, mask, filter, partitioning, content, agent groups, alerts, webhooks, roles, datasets, users, user-groups, AD, vIDM, vIDB, vSphere, NSX, proxies, time, CEIP, limits, trusted-certificates, config/data) has identical routes on both.

The swagger files at `/usr/lib/loginsight/application/3rd_party/apache-tomcat/webapps/ROOT/loginsight/api/swagger/*.json` give the request schemas. The 9.0-only ops-alerts, log-configs and data-migrations endpoints are not in swagger.

## Hands-on results

Password and session redacted. Every call sent `Authorization: Bearer <session>` and `Content-Type: application/json`. Every object was named `cfgmig-*` and has been deleted from both nodes since.

### Created on 8.18 (source)

| Object | Call | Result |
|---|---|---|
| Forwarder | `POST /api/v1/log-forwarder` `{"name":"cfgmig-fwd","host":"<9.0 node>","port":9543,"protocol":"cfapi","sslEnabled":true,"acceptCert":true,"transportProtocol":"TCP","workerCount":2,"filter":"appname=\"cfgmig\"","tags":{"cfgmig":"fwd"},"forwardComplementaryFields":true,"testConnection":true}` | 201, id `c4f02d71-39cd-31e6-b40c-caa5cf92c587`. Side effect: trusted cert `FORWARDER_<9.0 node>` added. |
| Mask rule | `POST /api/v1/log-mask` `{"name":"cfgmig-mask","enabled":true,"applyLogsFilter":"appname=\"cfgmig\"","fieldName":"text","selector":"secret=(\\S+)","maskValue":"****"}` | 201. The first try without a capture group got a 400 ("must have at least one capture group"). |
| Log filter | `POST /api/v1/log-filter` `{"name":"cfgmig-drop","enabled":false,"filter":"appname=\"cfgmig-drop-never\""}` | 201 |
| Agent group | `POST /api/v1/agent/groups` `{"name":"cfgmig-ag","info":"...","criteria":"hostname=\"cfgmig-host\"","agentConfig":"[filelog|cfgmig]\ndirectory=/var/log/cfgmig\ninclude=*.log\n"}` | 201. `name` and `criteria` are required, and the schema is undocumented. |
| Extracted field | `POST /api/v1/content/usercontent` (content-pack JSON with one `extractedFields` entry: `displayName`, `preContext`, `postContext`, `regexValue`, `constraints` as a JSON string) | 200. 8.18 has no direct field-create API. |
| Alert | `POST /api/v1/alerts` (flat v1 shape: `alertType:"RATE_BASED"`, `chartQuery` as a JSON string with `fieldConstraints:[{internalName:"appname",operator:"CONTAINS",value:"cfgmig"}]`, `enabled:false`) | 201 |
| Webhook | `POST /api/v2/notification/webhook` `{"name":"cfgmig-webhook","URLs":["https://192.0.2.10/cfgmig"],...}` | 200. A loopback URL was rejected ("URL can't be local"). |
| Dataset + role | `POST /api/v1/datasets`, `POST /api/v1/roles`, `PUT /api/v1/roles/:id/datasets {"dataSets":[id]}` | 201/201/200. The `datasets` key on role POST was ignored, so I used the PUT. |
| User dashboard | `POST /api/v1/content/usercontent` with one dashboard view copied from the vrops67 pack | 200 |

### Forwarder delivery 8.18 to 9.0: works

I sent one event into 8.18 with `POST /api/v1/events/ingest/cfgmig-agent` (appname `cfgmig`, text containing `user=bob secret=<test value>`).
- The event landed on the new worker <8.18 worker>. That node's CFAPIForwarder logged `An error occurred during forwarding: null` and retried with backoff for about 1.5 minutes. My best guess is that the trusted cert had not yet reached the just-joined node. It then delivered.
- 9.0 query `GET /api/v1/events/appname/CONTAINS%20cfgmig` returned the event with the original timestamp (14:09:27.541Z), original `source` <client ip>, `hostname`, `appname`, the forwarder tag `cfgmig=fwd`, and text already masked (`secret=****`). So masking runs before forwarding, and tags plus complementary fields go through.

### Recreating on 9.0 from 8.18 GET output

| Object | What I POSTed to 9.0 | Result |
|---|---|---|
| Forwarder (SSL cfapi, as-is from GET, host changed to .17, filter changed to never-match to avoid a loop) | `POST /api/v1/log-forwarder` | **495** "Forwarding destination certificate is not trusted. Use acceptCert:true". The same happened with `testConnection:false`. |
| Same, as syslog/TCP/514/no SSL | `POST /api/v1/log-forwarder` | **201**, the same id `c4f02d71-...` as on 8.18. `transportProtocol` came back lower-case `tcp`. |
| Mask rule (GET item verbatim, incl. id) | `POST /api/v1/log-mask` | **201**, same id |
| Mask rules (GET wrapper `{"logMaskRules":[...]}`) | `POST /api/v1/log-mask/batch` | **201**. A bare array gives 400. |
| Log filter (verbatim) | `POST /api/v1/log-filter` | **201**, same id |
| Agent group (GET item verbatim) | `POST /api/v1/agent/groups` | **201**, new random id |
| User content (8.18 `GET /api/v1/content/usercontent?namespace=...` output, `agentClasses` emptied) | `POST /api/v1/content/usercontent` | **200**. Extracted field and dashboard present, same internalName and same dashboard id. |
| Extracted field, 9.0-native | `POST /api/v2/fields/extractedFields {"name":..,"extractionRegex":..,"preContextRegex":..,"filterFieldName":"appname","filterOperator":"CONTAINS",...}` | **201** (that path returns 404 on 8.18) |
| Alert (8.18 v1 GET item verbatim) | `POST /api/v1/alerts` | **201**, new id, owner reset to caller |
| Webhook (v2 GET item verbatim) | `POST /api/v2/notification/webhook` | **200**, new id |
| Dataset (verbatim) | `POST /api/v1/datasets` | **201**, new id |
| Role (GET verbatim) | `POST /api/v1/roles` | **400** "Cannot deserialize ... capabilities[0]" |
| Role (capabilities flattened to strings, `dataSets` set to the new 9.0 dataset id) | `POST /api/v1/roles` | **201** |
| Content pack (8.18 `GET /api/v1/content/contentpack/com.vmware.vrops67`) | `POST /api/v1/content/contentpack` | **200**, installed intact |

### Bulk config export/import (`/api/v1/config/data`)

- `GET` returns a gzip tar holding `config_data.json`. That file has `ntpConfig`, `eventForwarderConfig`, `phoneHomeConfig`, `smtpConfig` (password in clear), `browserSessionConfig`, `languageConfig`, `alertsConfig` and `agentsGroupConfig`. Unlike the user-content export, the agent groups here include `criteria`.
- Import is two steps: POST the tarball with `Content-Type: application/octet-stream`, then POST `{"includeGeneralConfig":..,"includeNtpConfig":..,"includeEventForwarderConfig":..,"includeSmtpConfig":..,"includeArchivingConfig":..,"includeAgentsGroups":..}` as JSON.
- **The upload is broken on both 8.18 and 9.0.** Step 1 returns 200 "Successfully uploaded configuration file." Step 2 returns 500 "Cannot find the configuration file". The cause: `ConfigDataProvider.storeConfigFile` does `File.renameTo` from Play's temp file in `/tmp` (tmpfs) to `/usr/lib/loginsight/application/bin/config-data.tar.gz` (ext3 root). That is a cross-device rename, which fails silently.
- **Workaround, proven on 9.0:** `scp` the tarball to `/usr/lib/loginsight/application/bin/config-data.tar.gz` on the destination node, then send only step 2. The response was 202, per section: AgentsGroups "Successfully configured", EventForwarder "Failed to configure: The current instance cannot be a forwarding destination" (correct, since it pointed at itself). With the host edited to .17 the forwarder failed again on the untrusted cert, same as the REST case. I removed the file afterwards.

### Cleanup

All `cfgmig-*` objects are deleted on both nodes: forwarders, mask rules, log filters, agent groups, alerts, webhooks, roles, datasets, extracted fields, user dashboards. So are the vrops67 content pack on 9.0 and the auto-added trusted cert `FORWARDER_<9.0 node>` on 8.18, which was not there before the run. Verified by re-GET. Partition `secops` is untouched on both.

**Could not remove:** test events (the API cannot delete events).
- 8.18: the `cfgmig` test event, plus one `cfgmig-probe` unauthenticated ingest probe.
- 9.0: the forwarded `cfgmig` event, plus one `cfgmig-probe` probe.

## Recommended order for a config migration

1. **Auth and identity on 9.0 first, by hand:** AD bind / vIDB, then user groups. The secrets cannot be exported. Recreate local users with new passwords.
2. **Partitions** via `POST /api/v1/partitioning` (GET item verbatim; per-partition retention and archive go with it). Then restart loginsight on 9.0 once. Do this before any data or forwarding lands, so routing is right from the start.
3. **Log filters, then masking rules** (`/log-filter`, `/log-mask/batch` with the GET wrapper). Ingest-time processing should be in place before agents and forwarders point at 9.0.
4. **Content packs:** install the non-built-in ones from `GET /content/contentpack/:ns`. Skip general/vsphere/vsan, because 9.0 ships newer versions.
5. **Extracted fields and user/shared content** per user via `/content/usercontent` (and `/sharedcontent`). The internalNames come across unchanged, so dashboards and queries that reference them keep working.
6. **Datasets, then roles** (remap dataset ids, flatten capabilities), then **role assignment** on users and groups.
7. **Webhooks**, then **alerts**: v1 GET verbatim, with `webhookIds` remapped to the new webhook ids. Re-point any vROps-style alert targets to 9.0 Ops alerts.
8. **Agent groups** via `/agent/groups` (verbatim). Then retarget agents.
9. **SMTP, NTP, general settings:** NTP via `/time/config`. SMTP via the UI, or via the `config/data` bundle using the scp workaround (the file holds the SMTP password, so keep it off disk afterwards).
10. **Forwarders last,** with `acceptCert:true`, or pre-trust the targets via `/api/v2/trusted-certificates`. Last, so that 9.0 does not forward a mixed stream while you're still cutting over. Watch for loops if any target is the old cluster.
11. **Integrations from scratch:** vCenter (password), NSX, proxy credentials, and VCF Ops (from the Ops side; no vrops API in 9.0). Do appliance certificates and license separately.
