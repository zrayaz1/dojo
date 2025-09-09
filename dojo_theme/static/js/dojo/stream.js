/* global io */
(function () {
	var previewVideo = document.getElementById('preview-video');
	var startBtn = document.getElementById('start-stream-btn');
	var stopBtn = document.getElementById('stop-stream-btn');
	var captureTabBtn = document.getElementById('capture-tab-btn');
	var captureAudioBtn = document.getElementById('capture-audio-btn');
	var twitchKeyInput = document.getElementById('twitch-key');
	var qualitySelect = document.getElementById('stream-quality');
	var frameRateSelect = document.getElementById('frame-rate');
	var bitrateInput = document.getElementById('bitrate');
	var statusContainer = document.getElementById('stream-status');
	var statsContainer = document.getElementById('stream-stats');
	var fpsCounter = document.getElementById('fps-counter');
	var bitrateCounter = document.getElementById('bitrate-counter');
	var noPreview = document.getElementById('no-preview');

	var mediaStream = null;
	var mediaRecorder = null;
	var socket = null;
	var bytesSentThisSecond = 0;
	var fpsApprox = 0;
	var statsInterval = null;

	function setStatus(html, type) {
		statusContainer.innerHTML = '<div class="alert alert-' + (type || 'info') + '">' + html + '</div>';
	}

	function updateStatsUi() {
		bitrateCounter.textContent = Math.round((bytesSentThisSecond * 8) / 1000);
		fpsCounter.textContent = String(fpsApprox);
		bytesSentThisSecond = 0;
	}

	function startStats() {
		if (statsInterval) return;
		statsContainer.style.display = '';
		statsInterval = setInterval(updateStatsUi, 1000);
	}

	function stopStats() {
		if (!statsInterval) return;
		clearInterval(statsInterval);
		statsInterval = null;
		statsContainer.style.display = 'none';
	}

	function getConstraints() {
		var quality = qualitySelect.value;
		var width = 1280;
		var height = 720;
		if (quality === '480p') { width = 852; height = 480; }
		if (quality === '1080p') { width = 1920; height = 1080; }
		var fps = parseInt(frameRateSelect.value || '30', 10);
		return { width: width, height: height, frameRate: fps };
	}

	async function captureScreen() {
		try {
			var constraints = getConstraints();
			var screen = await navigator.mediaDevices.getDisplayMedia({
				video: { width: constraints.width, height: constraints.height, frameRate: constraints.frameRate },
				audio: false
			});
			return screen;
		} catch (e) {
			setStatus('<i class="fas fa-exclamation-triangle"></i> Screen capture error: ' + e.message, 'danger');
			throw e;
		}
	}

	async function captureMic() {
		try {
			var mic = await navigator.mediaDevices.getUserMedia({ audio: true, video: false });
			return mic;
		} catch (e) {
			setStatus('<i class="fas fa-exclamation-triangle"></i> Microphone error: ' + e.message, 'danger');
			throw e;
		}
	}

	function mixStreams(screenStream, micStream) {
		var tracks = [];
		if (screenStream) tracks = tracks.concat(screenStream.getVideoTracks());
		if (micStream) tracks = tracks.concat(micStream.getAudioTracks());
		return new MediaStream(tracks);
	}

	function getMimeType() {
		var preferred = 'video/webm;codecs=vp9,opus';
		if (MediaRecorder.isTypeSupported(preferred)) return preferred;
		preferred = 'video/webm;codecs=vp8,opus';
		if (MediaRecorder.isTypeSupported(preferred)) return preferred;
		return 'video/webm';
	}

	function connectSocket() {
		if (socket) return socket;
		var url = (window.STREAM_SOCKET_URL) || 'http://localhost:3100';
		/* eslint-disable no-undef */
		socket = io(url, { transports: ['websocket'] });
		/* eslint-enable no-undef */
		socket.on('connect', function () {
			setStatus('<i class="fas fa-signal"></i> Connected to streaming server', 'success');
		});
		socket.on('disconnect', function () {
			setStatus('<i class="fas fa-plug"></i> Disconnected from streaming server', 'warning');
		});
		socket.on('error', function (payload) {
			var msg = (payload && payload.message) ? payload.message : 'Unknown error';
			setStatus('<i class="fas fa-exclamation-circle"></i> ' + msg, 'danger');
			try { $('#error-message').text(msg); $('#errorModal').modal('show'); } catch (_) {}
		});
		return socket;
	}

	function startRecording(stream) {
		var mime = getMimeType();
		var kbps = parseInt(bitrateInput.value || '2500', 10);
		var ms = 250;
		try {
			mediaRecorder = new MediaRecorder(stream, { mimeType: mime, videoBitsPerSecond: kbps * 1000 });
		} catch (_) {
			mediaRecorder = new MediaRecorder(stream);
		}
		fpsApprox = parseInt(frameRateSelect.value || '30', 10);
		mediaRecorder.ondataavailable = function (e) {
			if (e && e.data && e.data.size > 0 && socket && socket.connected) {
				e.data.arrayBuffer().then(function (buf) {
					bytesSentThisSecond += buf.byteLength;
					socket.send(new Uint8Array(buf));
				});
			}
		};
		mediaRecorder.onstop = function () {};
		mediaRecorder.start(ms);
	}

	function stopRecording() {
		if (mediaRecorder && mediaRecorder.state !== 'inactive') mediaRecorder.stop();
		mediaRecorder = null;
	}

	function stopMedia() {
		if (mediaStream) {
			mediaStream.getTracks().forEach(function (t) { try { t.stop(); } catch (_) {} });
		}
		mediaStream = null;
	}

	async function startStream() {
		try {
			setStatus('<i class="fas fa-spinner fa-spin"></i> Preparing stream...', 'info');
			var screen = await captureScreen();
			var mic = null;
			try { if (captureAudioBtn.dataset.enabled === '1') mic = await captureMic(); } catch (_) {}
			mediaStream = mixStreams(screen, mic);
			noPreview && (noPreview.style.display = 'none');
			previewVideo.srcObject = mediaStream;
			connectSocket();
			startRecording(mediaStream);
			startBtn.style.display = 'none';
			stopBtn.style.display = '';
			startStats();
			setStatus('<i class="fas fa-broadcast-tower"></i> Streaming...', 'success');
		} catch (e) {
			setStatus('<i class="fas fa-exclamation-triangle"></i> ' + e.message, 'danger');
		}
	}

	function stopStream() {
		stopRecording();
		stopMedia();
		stopStats();
		startBtn.style.display = '';
		stopBtn.style.display = 'none';
		setStatus('<i class="fas fa-info-circle"></i> Ready to stream', 'info');
	}

	captureTabBtn.addEventListener('click', function () {
		captureScreen().then(function (screen) {
			if (mediaStream) stopMedia();
			mediaStream = screen;
			previewVideo.srcObject = mediaStream;
			noPreview && (noPreview.style.display = 'none');
		});
	});

	captureAudioBtn.addEventListener('click', function () {
		var enabled = captureAudioBtn.dataset.enabled === '1';
		captureAudioBtn.dataset.enabled = enabled ? '0' : '1';
		captureAudioBtn.classList.toggle('btn-info', enabled);
		captureAudioBtn.classList.toggle('btn-warning', !enabled);
	});

	startBtn.addEventListener('click', startStream);
	stopBtn.addEventListener('click', stopStream);
})();


