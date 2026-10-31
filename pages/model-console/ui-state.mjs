/** Pure draft helpers; snapshots never replace unsaved edits. */
export const resourceKey = r => `${r.kind}:${r.id}`;
export const draftKey = (r,path) => `${resourceKey(r)}:${path}`;
export function stage(drafts,resource,path,value,baseValue,revision){
 const next=new Map(drafts),key=draftKey(resource,path);
 if(value===baseValue)next.delete(key);
 else next.set(key,{resource,path,value,baseValue,revision});
 return next;
}
export function groups(drafts){const result=new Map();for(const d of drafts.values()){const key=resourceKey(d.resource);if(!result.has(key))result.set(key,{resource:d.resource,revision:d.revision,changes:[]});result.get(key).changes.push({path:d.path,value:d.value});}return [...result.values()];}
export function currentRevision(snapshot,resource){return resource.kind==='provider'?snapshot.models.find(m=>m.id===resource.id)?.revision:snapshot.resources.find(r=>resourceKey(r.resource)===resourceKey(resource))?.revision;}
export const candidates=(models,capability)=>models.filter(m=>m.enabled&&m.capabilities.includes(capability));
export const indexCapability=c=>['embedding','rerank'].includes(c);
export const isIssue=b=>['missing','disabled','field-missing','plugin-unavailable','value-unrecognized'].includes(b.status);
/** Secret API-key fields reviewed by the console; values never enter snapshots. */
export const secretFieldTypes={tavily:'secret-list',bocha:'secret-list',brave:'secret-list',firecrawl:'secret-list',exa:'secret-list',anysearch:'secret-list',deepseek:'secret-list',serpapi:'secret-list',baidu_ai_search:'secret-string'};
export const secretPaths={};
for(const [provider,type] of Object.entries(secretFieldTypes))secretPaths[`provider_settings.websearch_${provider==='baidu_ai_search'?'baidu_app_builder':provider}_key`]=type;
export const isSecretPath=path=>Object.hasOwn(secretPaths,path);
/** Masked summary of a secret draft value (JSON array for list, raw text for string). */
export function secretDraftText(path,value){
 const type=secretPaths[path];
 if(type!=='secret-list')return value&&String(value).trim()?'设置 Key（已隐藏）':'清空 Key';
 let list=[];
 try{list=JSON.parse(value||'[]');}catch{return '格式无效';}
 const count=Array.isArray(list)?list.filter(item=>typeof item==='string'&&item.trim()).length:0;
 return count>0?`设置 ${count} 个 Key（已隐藏）`:'清空 Key';
}
export function counts(snapshot){const modelBindings=r=>(r.bindings??[]).filter(b=>b.capability!=='setting');return {models:snapshot.models.filter(m=>m.enabled).length,resources:snapshot.resources.length,bindings:snapshot.resources.reduce((n,r)=>n+modelBindings(r).length,0),issues:snapshot.resources.reduce((n,r)=>n+(r.status==='unavailable'?1:(r.bindings??[]).filter(isIssue).length),0)};}

const OFFICIAL_WEB_SEARCH_LABELS={serpapi:'SerpApi',deepseek:'DeepSeek Search API',tavily:'Tavily',baidu_ai_search:'Baidu AI Search',bocha:'Bocha',brave:'Brave',firecrawl:'Firecrawl',exa:'Exa',anysearch:'AnySearch'};
export const webSearchProviderLabels={...OFFICIAL_WEB_SEARCH_LABELS};
/** Human-readable value for a web-search setting draft/review row. */
export function webSearchValueText(path,value){
 if(path==='provider_settings.web_search')return value==='true'?'已启用':value==='false'?'已关闭':String(value??'');
 if(path==='provider_settings.websearch_provider')return value==='model_native'?'跟随当前聊天模型（模型原生搜索）':OFFICIAL_WEB_SEARCH_LABELS[value]??String(value??'');
 return String(value??'');
}
/**
 * Card/section status for the web-search settings group.
 * The native option is only meaningful when the plugin was detected and activated;
 * if the stored value is model_native without an active plugin, the group is shown as an issue.
 */
export function webSearchStatus(settings,features){
 const enabled=String(settings.find(b=>b.path==='provider_settings.web_search')?.value??'');
 const provider=settings.find(b=>b.path==='provider_settings.websearch_provider');
 const value=provider?.value??'';
 const native=features?.nativeWebSearch;
 const option=(provider?.options??[]).find(item=>item.value===value);
 if(value==='model_native'&&!native?.available){
  return {label:'模型原生 · 插件未启用',kind:'bad',noteClass:'note-warn',note:native?.installed?'检测到模型原生搜索插件但当前未启用：配置为“模型原生搜索”时搜索不会生效，请启用插件或改回内置提供商。':'当前配置是“模型原生搜索”，但未检测到模型原生搜索插件：搜索不会生效，请改回内置提供商。'};
 }
 if(enabled==='false')return {label:'网页搜索 · 已关闭',kind:'',noteClass:'note-info',note:'开关关闭时不会向模型提供网页搜索工具；已选择的提供商会被保留。'};
 if(value==='model_native')return {label:'模型原生 · 已就绪',kind:'good',noteClass:'note-good',note:`检测到 astrbot_plugin_model_websearch${native?.version?` ${native.version}`:''}（已启用）。搜索将使用当前会话聊天模型的原生联网能力，不再依赖第三方搜索 Key。`};
 if(option)return {label:`内置 · ${OFFICIAL_WEB_SEARCH_LABELS[value]??option.label}`,kind:'purple',noteClass:'note-info',note:'使用 AstrBot 内置搜索 API；需要先在 AstrBot 中配置对应提供商的 API Key。'};
 if(!native?.listAvailable)return {label:'插件列表不可读',kind:'warn',noteClass:'note-info',note:'无法读取 AstrBot 插件列表，因此只显示内置搜索提供商；远程设备可能需要插件读取权限。'};
 return {label:'提供商未识别',kind:'warn',noteClass:'note-warn',note:'当前网页搜索提供商不在已审查列表内，只能改回已知值。'};
}

/**
 * Device scope for bridge calls: the plugin-relative endpoint never carries a query string.
 * GET endpoints receive `{device}` as params; POST endpoints put the same field in the body
 * (the Dashboard bridge has no query channel for POST). 'local' (or no selection) sends nothing.
 */
export function deviceParams(deviceId){return deviceId&&deviceId!=='local'?{device:deviceId}:undefined;}

/** Facts rendered by the plugin state card; `state` is the read-only GET state payload. */
export function stateFacts(state){
 if(!state||typeof state!=='object')return [];
 const facts=[];
 const push=(label,value,kind='')=>{if(value===undefined||value===null||value==='')return;facts.push({label,value:String(value),kind});};
 push('状态目录',state.stateDir);
 push('AstrBot 数据目录',state.astrbotData);
 if(state.legacyStateDir)push('旧状态目录',state.legacyStateDir);
 push('旧状态导入',state.legacyImported===true?'已导入':state.legacyImported===false?'未导入':'未知',state.legacyImported===true?'good':state.legacyImported===false?'warn':'');
 push('编辑模式',state.editMode===false?'只读（edit_mode=false）':'可写',state.editMode===false?'warn':'good');
 push('状态目录写入',state.writable===true?'可写':state.writable===false?'不可写':'未知',state.writable===true?'good':state.writable===false?'bad':'');
 return facts;
}

/** Human summary for POST state/migrate (manual legacy-state import). */
export function migrateResultText(result){
 if(!result||typeof result!=='object')return '迁移请求已完成，但未返回结果。';
 const files=Number.isFinite(Number(result.files))?Number(result.files):0;
 const skipped=Number.isFinite(Number(result.skipped))?Number(result.skipped):0;
 if(result.imported===false)return `没有需要导入的旧状态（跳过 ${skipped} 个文件）。`;
 return `旧状态导入完成：写入 ${files} 个文件，跳过 ${skipped} 个已存在文件。`;
}
/** Writable snapshots are authoritative: remote agents are only enabled by snapshot.writeEnabled. */
export function snapshotWritable(snapshot){return snapshot?.writeEnabled===true;}
/** Header mode label: remote writable agents get “远程 · 可写”, everything else stays read-only. */
export function snapshotMode(snapshot){
 if(!snapshot)return {text:'未连接',kind:''};
 const remote=isRemoteSnapshot(snapshot),writable=snapshotWritable(snapshot);
 if(remote)return writable?{text:'远程 · 可写',kind:'good'}:{text:'远程 · 只读',kind:'purple'};
 return writable?{text:'允许保存',kind:'good'}:{text:'只读模式',kind:'purple'};
}
/** Hot-load evidence from Agent apply: never claim success without verified log evidence. */
export function hotLoadMessage(hotLoad){
 if(!hotLoad||typeof hotLoad!=='object')return '';
 const note=typeof hotLoad.note==='string'&&hotLoad.note.trim()?`：${hotLoad.note.trim()}`:'';
 if(hotLoad.verified===true)return `热加载已核实（日志出现重载标记）${note}`;
 if(hotLoad.checked===true)return `热加载未核实（已检查日志，未见重载标记）${note}`;
 return `热加载未核实（未检查日志）${note}`;
}
/** A remote snapshot is read-only; missing device means the legacy local view. Epic-2: ssh/agent are remote too. */
export function isRemoteSnapshot(snapshot){const kind=snapshot?.device?.kind;return typeof kind==='string'&&kind.length>0&&kind!=='local';}
/** Normalized device kind: legacy records without kind are http, 'local' is the built-in local device. */
export function deviceKind(device){if(!device||typeof device!=='object')return 'http';if(device.kind==='local'||device.id==='local')return 'local';return device.kind==='ssh'||device.kind==='agent'?device.kind:'http';}
export const deviceKindLabels={local:'本机',http:'HTTP',ssh:'SSH',agent:'Agent'};
export function deviceKindLabel(kind){return deviceKindLabels[kind]??deviceKindLabels.http;}
/** Device selector options: local first, then remote devices; falls back to local-only before /devices is available. */
export function deviceOptions(devices){
 const list=Array.isArray(devices)?devices.filter(device=>device&&typeof device.id==='string'&&device.id):[];
 const local=list.find(device=>device.kind==='local'||device.id==='local');
 const options=[{value:'local',label:`本机${local?(local.readOnly?' · 只读':' · 可写'):''}`,kind:'local',readOnly:Boolean(local?.readOnly)}];
 for(const device of list){if(local&&device.id===local.id)continue;if(device.id==='local')continue;options.push({value:device.id,label:device.name??device.id,kind:device.kind??'http',readOnly:device.readOnly!==false});}
 return options;
}
/** Readable device health badge for the settings table. */
export function healthBadge(health){
 if(!health||typeof health!=='object')return {text:'未检测',kind:'warn'};
 if(health.reachable===true)return {text:Number.isFinite(health.latencyMs)?`可达 · ${health.latencyMs}ms`:'可达',kind:'good'};
 return {text:typeof health.error==='string'&&health.error?health.error:'不可达',kind:'bad'};
}
/** Readable device errors; empty string means the code is not device-related. */
export const deviceErrors={DEVICE_AUTH_FAILED:'远程设备凭据无效或已被撤销，请在设备设置中更新 OpenAPI Key。',DEVICE_FORBIDDEN:'远程设备缺少所需权限（scope），请检查 OpenAPI Key 的授权范围。',DEVICE_UNREACHABLE:'远程设备不可达，请确认目标机 AstrBot 正在运行且地址可访问。',DEVICE_NOT_FOUND:'设备不存在，可能已被删除。',DEVICE_INVALID:'设备信息不合法，请检查名称与地址。',DEVICE_SECRET_REQUIRED:'添加 HTTP/SSH 设备需要 OpenAPI Key。',DEVICE_EXISTS:'设备已存在（同类型且地址相同），请换一个名称或地址。',DEVICE_INVALID_RESPONSE:'远程设备返回了无法解析的响应。',DEVICE_API_REJECTED:'远程 AstrBot 拒绝了该请求。',DEVICE_SSH_UNAVAILABLE:'本机未找到 ssh 命令，无法建立 SSH 隧道。',DEVICE_SSH_FAILED:'SSH 隧道建立或转发失败，请检查主机、端口、用户与私钥路径。',DEVICE_PORT_UNKNOWN:'无法从远端日志发现 dashboard 端口，请确认远端数据目录，或在地址中直接填写端口。',AGENT_UNREACHABLE:'Agent 不可达，请确认 Agent 进程正在运行且地址与端口正确。',AGENT_PAIR_FAILED:'Agent 配对失败，请检查配对码是否有效（一次性、10 分钟内有效）。',AGENT_AUTH_FAILED:'Agent 设备令牌无效或已失效，请删除设备后重新配对。',AGENT_TOKEN_EXPIRED:'Agent 设备令牌已过期，请重新配对。',AGENT_INVALID_RESPONSE:'Agent 返回了无法解析的响应。',DEVICE_READ_ONLY:'该设备为只读，不能执行写入操作。',AGENT_READ_ONLY:'Agent 端未启用写入（--enable-writes），无法保存；请先在 Agent 侧开启写入。',DEVICE_NAME_REQUIRED:'请填写设备名称。',DEVICE_BASEURL_REQUIRED:'请填写设备地址（baseUrl）。',DEVICE_PAIRING_REQUIRED:'请填写 Agent 配对码。',DEVICE_SSH_HOST_REQUIRED:'SSH 设备需要填写主机地址。',DEVICE_SSH_USER_REQUIRED:'SSH 设备需要填写登录用户。'};
export function deviceErrorText(code){return typeof code==='string'?deviceErrors[code]??'':'';}
/** Validate the device form and build the POST /devices payload (kind defaults to http for legacy forms). */
export function validateDeviceInput(input){
 const kind=input?.kind==='ssh'||input?.kind==='agent'?input.kind:'http';
 const name=typeof input?.name==='string'?input.name.trim():'';
 const baseUrl=typeof input?.baseUrl==='string'?input.baseUrl.trim():'';
 const errors=[],payload={name,kind,baseUrl};
 payload.readOnly=kind==='agent'?input?.allowWrite!==true:true;
 if(!name)errors.push('DEVICE_NAME_REQUIRED');
 if(!baseUrl)errors.push('DEVICE_BASEURL_REQUIRED');
 const apiKey=typeof input?.apiKey==='string'?input.apiKey.trim():'';
 if(kind==='http'||kind==='ssh'){if(!apiKey)errors.push('DEVICE_SECRET_REQUIRED');else payload.apiKey=apiKey;}
 if(kind==='agent'){const pairingCode=typeof input?.pairingCode==='string'?input.pairingCode.trim():'';if(!pairingCode)errors.push('DEVICE_PAIRING_REQUIRED');else payload.pairingCode=pairingCode;}
 if(kind==='ssh'){
  const host=typeof input?.host==='string'?input.host.trim():'';
  const user=typeof input?.user==='string'?input.user.trim():'';
  if(!host)errors.push('DEVICE_SSH_HOST_REQUIRED');
  if(!user)errors.push('DEVICE_SSH_USER_REQUIRED');
  const ssh={host,user};
  const port=Number(input?.port);
  if(Number.isInteger(port)&&port>0&&port<=65535)ssh.port=port;
  if(typeof input?.keyPath==='string'&&input.keyPath.trim())ssh.keyPath=input.keyPath.trim();
  if(typeof input?.dataPath==='string'&&input.dataPath.trim())ssh.dataPath=input.dataPath.trim();
  payload.ssh=ssh;
 }
 return {ok:errors.length===0,errors,payload};
}
/** Recent BACKUP_* warnings from GET /backups, capped for display. */
export function backupWarnings(warnings,limit=5){
 return (Array.isArray(warnings)?warnings:[]).filter(warning=>warning&&(warning.code||warning.message)).slice(0,Math.max(0,limit)).map(warning=>({code:typeof warning.code==='string'&&warning.code?warning.code:'BACKUP_WARNING',time:typeof warning.time==='string'?warning.time:'',message:typeof warning.message==='string'?warning.message:''}));
}
/** Snapshot config version shown in the header (RISK-05). */
export function configVersionText(snapshot){const value=snapshot?.configVersion;return `配置版本 ${value===undefined||value===null||value===''?'未知':String(value)}`;}

/**
 * Plan warning presentation for the confirm dialog.
 * Engine plans always carry the same English caveat; showing it as a red error on every apply
 * looked like a failure. Apply warnings become neutral, localized info; restore keeps the red
 * destructive warning (whole-object overwrite).
 */
export function planWarning(warning,{restore=false}={}){
 const raw=typeof warning==='string'?warning.trim():'';
 if(restore)return {text:raw||'恢复会覆盖所选资源的完整对象（不只是下列差异字段）。请确认备份内容与差异后再应用。',kind:'danger'};
 if(!raw)return null;
 const boilerplate='No cross-resource atomicity; API readback is not a model generation test';
 return {text:raw===boilerplate?'多资源按顺序保存，不是原子操作；保存并回读成功不代表模型已能生成。':raw,kind:'info'};
}

/** Binding status pills. field-missing means the path is absent from config, not an unverified empty value. */
export const bindingStatuses={configured:{label:'已配置',kind:'good'},off:{label:'已关闭',kind:''},empty:{label:'未配置',kind:''},missing:{label:'引用缺失',kind:'bad'},disabled:{label:'模型禁用',kind:'bad'},'field-missing':{label:'字段不存在',kind:'bad'},'plugin-unavailable':{label:'插件未启用',kind:'bad'},'value-unrecognized':{label:'值未识别',kind:'warn'},'per-session':{label:'跟随会话',kind:'warn'},'empty-semantics-unverified':{label:'待核实',kind:'warn'}};
export function bindingStatus(status){const known=bindingStatuses[status];return known?{...known}:{label:String(status??'未知'),kind:'warn'};}

/** A session/wildcard route exists, so cards only describe the platform-level view. */
export function routingNoticeText(routing){
 const granular=Number(routing?.granular??0);
 if(!(granular>0))return '';
 const total=Number.isFinite(Number(routing?.total))?Number(routing.total):granular;
 return `本设备存在会话级/通配符路由（共 ${total} 条路由，其中细分路由 ${granular} 条），卡片仅代表平台级视图。`;
}

const displayValue=value=>value===undefined||value===null||value===''?'空值 / 跟随默认':String(value);
/** Conflict diff: align local drafts with the latest server values. Missing server paths mean the field does not exist. */
export function buildConflictRows(pending,current,labels={}){
 const server=new Map();
 if(Array.isArray(current)){for(const item of current)if(item&&typeof item.path==='string')server.set(item.path,item.value);}
 else if(current&&typeof current==='object'){for(const [path,value] of Object.entries(current))server.set(path,value);}
 const rows=[];
 for(const change of Array.isArray(pending)?pending:[]){
  if(!change||typeof change.path!=='string')continue;
  const mine=displayValue(change.value);
  const serverValue=server.has(change.path)?displayValue(server.get(change.path)):'字段不存在';
  rows.push({path:change.path,label:labels[change.path]??change.path,mine,server:serverValue,changed:mine!==serverValue});
 }
 return rows;
}

/** Instance cards use their effective profile resource; unassigned archives never substitute for bots. */
export function profileViews(resources){
 const profiles=resources.filter(r=>r.resource.kind==='profile'),bots=[],configs=[],unassigned=[];
 for(const item of profiles){const identity=item.resource.identity;for(const instance of identity?.instances??[]){bots.push({...item,viewGroup:'bot',viewKey:'bot:'+instance.instanceId,viewName:instance.instanceId,viewSubtitle:instance.category+' · 类型：'+instance.type+' · '+(item.resource.id==='default'?'使用全局默认配置（default）':'使用专用配置（'+item.resource.id.slice(0,8)+'）')+' · 名称来源：AstrBot实例标识'});}
 if(item.resource.id==='default')configs.push({...item,viewGroup:'shared',viewKey:'shared:default',viewName:'全局默认配置（共享）',viewSubtitle:'修改将影响所有使用 default 的机器人与会话'});
 else if(!identity?.instances?.length)unassigned.push({...item,viewGroup:'unassigned',viewKey:'archive:'+item.resource.id,viewName:'未使用的配置档案 · '+item.resource.id.slice(0,8),viewSubtitle:'未关联机器人实例；独立保留，不代表任何已接入bot'});
 }
 return [...bots,...configs,...unassigned];
}
