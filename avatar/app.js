const find = selector => document.querySelector(selector);
const video = find('#remote-video'); const audio = find('#remote-audio');
let csrf = ''; let current = null; let busy = 0; let stopping = false;
let room = null; let info = null; let joined = false; let epoch = 0;
let ownedMic = null; let pendingMic = null; let micEpoch = 0;
let waiting = false; let sentAtTurn = 0; let refreshRevision = 0;
const stoppedTracks = new WeakSet(); const cleanups = new WeakMap(); const listeners = new WeakMap();
const states = {prepared:'Preparado',starting:'Preparando recursos propios',ready:'Sala lista',active:'Agente consultado',stopping:'Finalizando',ended:'Cierre confirmado',failed:'Inicio no completado',reconcile_required:'Revisión de recursos pendiente'};
const stages = {create_room:'crear la sala LiveKit',check_dispatch:'verificar los agentes de LiveKit',connect_room:'conectar con la sala LiveKit',create_pal:'preparar el avatar Tavus',create_conversation:'iniciar la conversación Tavus',connect_agent:'conectar el agente de voz',runtime_stt:'reconocer la voz con ElevenLabs',runtime_tts:'sintetizar la respuesta con ElevenLabs',runtime_llm:'consultar el agente original',runtime_session:'mantener la sesión de voz'};
const causes = {timeout:'se agotó el tiempo de espera',cancelled:'el inicio fue cancelado',connection_error:'falló la conexión',authentication_failed:'el proveedor rechazó la autorización',rate_limited:'el proveedor limitó las solicitudes',provider_rejected:'el proveedor rechazó la solicitud',provider_error:'el proveedor devolvió un error',invalid_response:'la respuesta del proveedor no fue válida',unexpected_dispatch:'se detectó una asignación de agentes inesperada',unexpected_participant:'se detectó un participante no autorizado',internal_error:'ocurrió un error interno'};
function fail(message) {
  const error = find('#error');
  if (message || error.hidden || !error.textContent) error.textContent = message || 'No se pudo completar la operación. Revise el diagnóstico y la guía; no inicie otra prueba.';
  error.hidden = false;
}
async function api(path, body) {
  const result = await fetch(path, {method:body === undefined ? 'GET' : 'POST',
    headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},...(body === undefined ? {} : {body:JSON.stringify(body)})});
  if (!result.ok) throw new Error('request_unavailable');
  return result.status === 204 ? null : result.json();
}
function render() {
  if (!current) return;
  find('#state').textContent = states[current.status] || 'Estado no disponible';
  const failure = current.failure; const diagnostic = find('#diagnostic');
  diagnostic.hidden = true;
  if (failure) {
    find('#state').textContent = failure.stage?.startsWith('runtime_') ? 'La sesión se interrumpió' : failure.code === 'cancelled' ? 'Inicio cancelado' : 'No se pudo iniciar el avatar';
    const http = Number.isInteger(failure.http_status) && failure.http_status >= 400 && failure.http_status <= 599 ? ` (HTTP ${failure.http_status})` : '';
    diagnostic.textContent = `Al ${stages[failure.stage] || 'preparar la sesión'}: ${causes[failure.code] || causes.internal_error}${http}. ${current.cleanup_confirmed ? 'Limpieza remota confirmada.' : 'La limpieza remota requiere revisión.'} La reserva sigue consumida. Consulte la guía de diagnóstico.`;
    diagnostic.hidden = false;
  } else if (current.trial_consumed && current.diagnostic_available === false && ['ended','failed','reconcile_required'].includes(current.status)) {
    diagnostic.textContent = 'Este registro anterior no conserva el motivo del cierre. El cierre no demuestra que el avatar haya funcionado. No se iniciará otra prueba.';
    diagnostic.hidden = false;
  }
  find('#budget').textContent = `Tiempo restante: ${current.remaining_seconds} s. Respuestas del agente: ${current.completed_turns}.`;
  find('#detail').textContent = current.join_available && !joined ? 'La sala ya está creada. Puede unirse mientras se prepara el avatar.' : current.trial_consumed ? 'La reserva ya fue utilizada. No se inicia otra sesión.' : !current.dispatch_isolation_confirmed ? 'El operador debe verificar el aislamiento del proyecto LiveKit. No se crearán recursos.' : !current.enabled ? 'Las sesiones reales están desactivadas en la configuración privada.' : !window.LivekitClient ? 'El cliente de LiveKit no se ha cargado. Revise la conexión y recargue esta página antes de iniciar; no se han reservado recursos.' : 'Iniciar reserva una única prueba. Finalizar requiere confirmación remota.';
  find('#start').disabled = busy > 0 || current.start_pending || !current.enabled || !current.dispatch_isolation_confirmed || current.trial_consumed || !find('#consent').checked || !window.LivekitClient;
  find('#join').hidden = !current.join_available;find('#join').disabled = busy > 0 || joined;
  find('#stop').disabled = stopping || !current.trial_consumed || current.status === 'ended';
  find('#microphone').disabled = busy > 0 || !joined || !['ready','active'].includes(current.status);
  find('#message').disabled = !joined || waiting || !['ready','active'].includes(current.status);
  find('#send').disabled = find('#message').disabled;
}
async function refresh() {
  const revision = ++refreshRevision;const generation = epoch;
  const update = await api('/api/status');
  if (revision !== refreshRevision || generation !== epoch) return;
  current = update;
  if (waiting && current.completed_turns > sentAtTurn) waiting = false;
  if (['ended','failed','reconcile_required'].includes(current.status) && room) {
    if (!joined && !stopping) fail('La sesión finalizó antes de completar la conexión con la sala. Revise el diagnóstico; la prueba no se reinicia.');
    await dispose();
  }
  render();
}
async function action(operation, localFailure = null) {
  busy++;render();if (!stopping) find('#error').hidden = true;
  try {await operation();} catch {
    fail(localFailure || undefined);
    if (!localFailure && current?.trial_consumed && !stopping) {
      stopping = true;
      try {await dispose();current = await api('/api/stop',{});} catch {fail();} finally {stopping = false;}
    }
  }
  finally {busy--;try {await refresh();} catch {fail();}}
}
function stopTrack(track) {if (track && !stoppedTracks.has(track)) {stoppedTracks.add(track);track.stop();}}
function releaseMic() {
  micEpoch++; const tracks = [ownedMic,pendingMic];ownedMic = pendingMic = null;
  for (const track of tracks) stopTrack(track);
  find('#microphone').textContent = 'Activar mi micrófono';
  return tracks.filter(Boolean);
}
async function dispose() {
  ++epoch; const target = room;room = null;joined = false;info = null;waiting = false;
  const tracks = releaseMic();
  for (const media of [audio,video]) {media.pause();media.srcObject = null;}
  video.hidden = true;find('#empty').hidden = false;
  for (const key of ['audioPlayback','videoPlayback','audioProgress','videoProgress']) delete find('#stage').dataset[key];
  if (!target || cleanups.has(target)) return target ? cleanups.get(target) : undefined;
  for (const [event,handler] of listeners.get(target) || []) target.off(event,handler);
  listeners.delete(target);
  const work = (async () => {for (const track of tracks) {try {await target.localParticipant.unpublishTrack(track,false);} catch {}}
    await target.disconnect();})();cleanups.set(target,work);return work;
}
function stop() {
  if (stopping) return;
  stopping = true;const local = dispose().catch(() => fail());
  return action(async () => {try {current = await api('/api/stop',{});} finally {await local;stopping = false;}});
}
function expectedAvatar(participant) {return info && participant?.identity === info.avatar_identity && participant.attributes?.['lk.publish_on_behalf'] === info.agent_identity;}
function known(participant) {return info && [info.owner_identity,info.agent_identity,info.avatar_identity].includes(participant?.identity);}
function subscribe(participant) {
  if (!known(participant)) {fail('La conexión se interrumpió al detectar un participante no autorizado. Se solicitó finalizar la sesión. Revise el diagnóstico; la prueba no se reinicia.');stop();return;}
  for (const publication of participant.trackPublications.values()) publication.setSubscribed(Boolean(expectedAvatar(participant)));
}
find('#unlock-form').addEventListener('submit',async event => {
  event.preventDefault();const key = find('#owner-key');
  try {csrf = (await api('/api/bootstrap')).csrf;await api('/api/unlock',{key:key.value});find('#unlock').hidden = true;find('#controls').hidden = false;await refresh();
    setInterval(() => {refresh().catch(() => fail());},2000);find('#consent').focus();} catch {fail();} finally {key.value = '';}
});
find('#consent').addEventListener('change',render);
find('#start').addEventListener('click',() => action(async () => {current = {...current,trial_consumed:true};current = await api('/api/start',{confirmed:true});}));
find('#join').addEventListener('click',() => action(async () => {
  const generation = ++epoch;let target = null;
  find('#room-status').textContent = 'Conectando con la sala…';
  try {
    const value = await api('/api/join',{});if (generation !== epoch || stopping) return;
    if (['ended','failed','reconcile_required'].includes(current.status)) {fail('La sesión finalizó antes de completar la conexión con la sala. Revise el diagnóstico; la prueba no se reinicia.');return;}
    target = new window.LivekitClient.Room();room = target;info = value;
    const live = () => generation === epoch && room === target && !stopping;
    const on = (event,handler) => {const guarded = (...args) => {if (live()) handler(...args);};target.on(event,guarded);const group = listeners.get(target) || [];group.push([event,guarded]);listeners.set(target,group);};
    const events = window.LivekitClient.RoomEvent;
    if (events.SignalConnected) on(events.SignalConnected,() => {find('#room-status').textContent = 'Acceso a la sala confirmado. Estableciendo la conexión multimedia…';});
    on(events.ParticipantConnected,subscribe);
    on(events.TrackPublished,(_publication,participant) => subscribe(participant));
    on(events.TrackSubscribed,(track,publication,participant) => {
      if (!expectedAvatar(participant)) {publication.setSubscribed(false);if (!known(participant)) {fail('La conexión se interrumpió al detectar un participante no autorizado. Se solicitó finalizar la sesión. Revise el diagnóstico; la prueba no se reinicia.');stop();}return;}
      const element = track.kind === 'audio' ? audio : track.kind === 'video' ? video : null;
      if (!element) return;
      track.attach(element);if (element === video) {video.hidden = false;find('#empty').hidden = true;}
      element.play().catch(() => {if (live()) find('#sound').hidden = false;});
    });
    on(events.TrackUnsubscribed,(track,_publication,participant) => {if (expectedAvatar(participant)) track.detach(track.kind === 'audio' ? audio : video);});
    on(events.Disconnected,() => {fail('Se interrumpió la conexión con la sala. Se solicitó finalizar la sesión. Revise el diagnóstico; la prueba no se reinicia.');stop();});
    await target.connect(value.url,value.token,{autoSubscribe:false,maxRetries:0});
    if (!live()) {await target.disconnect();return;}
    joined = true;for (const participant of target.remoteParticipants.values()) subscribe(participant);
    find('#room-status').textContent = 'Conexión multimedia establecida. Esperando el vídeo del avatar autorizado…';
  } catch {const interrupted = generation !== epoch || stopping;
    if (target === room && generation === epoch) await dispose();else if (target) await target.disconnect();
    if (!interrupted) throw new Error('join_unavailable');}
}));
find('#microphone').addEventListener('click',() => action(async () => {
  const target = room;const generation = epoch;const request = ++micEpoch;
  if (!target || !joined) throw new Error('not_joined');
  if (ownedMic) {const tracks = releaseMic();for (const track of tracks) await target.localParticipant.unpublishTrack(track,false);return;}
  const live = () => target === room && generation === epoch && request === micEpoch && joined && !stopping;
  const track = await window.LivekitClient.createLocalAudioTrack({echoCancellation:true,noiseSuppression:true});
  if (!live()) {stopTrack(track);return;}
  pendingMic = track;
  try {target.localParticipant.setTrackSubscriptionPermissions(false,[{participantIdentity:info.agent_identity,allowAll:true}]);
    await target.localParticipant.publishTrack(track,{source:window.LivekitClient.Track.Source.Microphone});
    if (!live()) {await target.localParticipant.unpublishTrack(track,false);throw new Error('session_stopped');}
    ownedMic = track;pendingMic = null;find('#microphone').textContent = 'Desactivar mi micrófono';
  } catch {if (pendingMic === track) pendingMic = null;stopTrack(track);throw new Error('microphone_unavailable');}
}, 'No se pudo activar el micrófono. Revise los permisos y vuelva a pulsar su botón, o continúe escribiendo.'));
find('#message-form').addEventListener('submit',event => {event.preventDefault();if (!room || waiting) return;
  const input = find('#message');const text = input.value.trim();if (!text || text.length > 2000) return;
  waiting = true;sentAtTurn = current.completed_turns;
  room.localParticipant.sendText(text,{topic:'lk.chat',destinationIdentities:[info.agent_identity]}).then(() => {input.value = '';}).catch(() => {fail();stop();});render();
});
find('#stop').addEventListener('click',stop);
find('#sound').addEventListener('click',() => action(async () => {find('#sound').hidden = false;await audio.play();await video.play();find('#sound').hidden = true;}, 'No se pudo activar el sonido. Vuelva a pulsar Activar sonido o continúe escribiendo.'));
for (const [element,type] of [[audio,'audio'],[video,'video']]) {
  element.addEventListener('playing',() => {find('#stage').dataset[type + 'Playback'] = 'playing';});
  element.addEventListener('timeupdate',() => {if (element.currentTime > 0) find('#stage').dataset[type + 'Progress'] = 'observed';});
}
window.addEventListener('pagehide',() => {dispose().catch(() => fail());if (current?.trial_consumed) fetch('/api/stop',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},body:'{}',keepalive:true}).catch(() => {});});
