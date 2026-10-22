/**
 * Cảnh đường phố 3D chỉ đọc snapshot đã xác minh cho ba kênh đèn đường.
 * Hỗ trợ sơ đồ 2D khi WebGL không khả dụng; không phát lệnh điều khiển.
 */
(function (window) {
  'use strict';

  var LAMP_IDS = ['lamp-01', 'lamp-02', 'lamp-03'];
  var LAMP_LABELS = {
    'lamp-01': { code: 'L1', name: 'Đèn đường 01', zoneName: 'Cột đầu tuyến' },
    'lamp-02': { code: 'L2', name: 'Đèn đường 02', zoneName: 'Cột giữa tuyến' },
    'lamp-03': { code: 'L3', name: 'Đèn đường 03', zoneName: 'Cột cuối tuyến' }
  };

  function createDefaultLampState(id) {
    return {
      id: id,
      available: false,
      stale: true,
      state: null,
      mode: null,
      currentMa: null,
      voltageV: null,
      lastUpdatedTs: null,
      hasVerifiedData: false,
      sourceTs: null,
      sensorValid: false,
    };
  }

  function ClassroomTwinApp(containerEl) {
    this.root = containerEl;
    this.selectedLampId = 'lamp-01';
    this.renderMode = 'init'; // 'webgl' | '2d'
    this.isLowEndDevice = this.detectLowEndDevice();
    this.disposed = false;

    // Trạng thái hệ thống đã chuẩn hóa
    this.twinState = {
      lux: null,
      luxValid: false,
      autoOffLux: null,
      gatewayPending: false,
      gatewayStatus: null,
      lastSnapshotTime: null,
      systemModeSummary: 'UNKNOWN',
      hasSnapshot: false,
      streamStale: false,
      lastReceivedAt: null,
      lamps: {
        'lamp-01': createDefaultLampState('lamp-01'),
        'lamp-02': createDefaultLampState('lamp-02'),
        'lamp-03': createDefaultLampState('lamp-03')
      }
    };

    // Tham chiếu đối tượng Three.js
    this.three = {
      scene: null,
      camera: null,
      renderer: null,
      raycaster: null,
      pointer: null,
      lampObjects: {},
      clickableMeshes: [],
      animFrameId: null
    };

    // Góc camera phối cảnh nhìn dọc ba cột đèn trên tuyến đường
    this.cameraOrbit = {
      azimuth: 0.52,      // rad (~30 độ xoay)
      polar: 0.95,        // rad (~54 độ nhìn xéo từ trên xuống)
      radius: 12.8,       // Khoảng cách zoom tối ưu
      target: { x: 0, y: 1.5, z: 0 },
      isDragging: false,
      startX: 0,
      startY: 0
    };

    this.initDOM();
    this.initSceneOrFallback();
    this.bindEvents();
    this.renderUI();
    var self = this;
    this.freshnessTimer = window.setInterval(function () {
      if (self.twinState.lastReceivedAt && Date.now() - self.twinState.lastReceivedAt > 16000 && !self.twinState.streamStale) {
        self.twinState.streamStale = true;
        self.sync3DLampsVisual();
        self.renderUI();
      }
    }, 1000);
  }

  ClassroomTwinApp.prototype.detectLowEndDevice = function () {
    var cores = navigator.hardwareConcurrency || 4;
    var mem = navigator.deviceMemory || 4;
    return cores <= 2 || mem <= 2;
  };

  ClassroomTwinApp.prototype.initDOM = function () {
    this.elViewportWrap = this.root.querySelector('.dt-viewport-wrap');
    this.elCanvasHost = this.root.querySelector('.dt-webgl-host');
    this.elFallback2D = this.root.querySelector('.dt-fallback-2d');
    this.elLoading = this.root.querySelector('.dt-loading-overlay');
    this.elBanner = this.root.querySelector('.dt-gateway-banner');
    this.elLuxBadge = this.root.querySelector('.dt-lux-badge');
    this.elThresholdBadge = this.root.querySelector('.dt-threshold-badge');
    this.elModeBadge = this.root.querySelector('.dt-mode-badge');
    this.elCardsWrap = this.root.querySelector('.dt-lamp-cards');
    this.elDetailPane = this.root.querySelector('.dt-detail-pane');
    this.btnResetCam = this.root.querySelector('.dt-btn-reset-cam');
    this.btnToggle2D = this.root.querySelector('.dt-btn-toggle-2d');
  };

  ClassroomTwinApp.prototype.initSceneOrFallback = function () {
    if (window.THREE && this.checkWebGLSupport()) {
      try {
        this.initWebGL();
        this.renderMode = 'webgl';
        if (this.elFallback2D) this.elFallback2D.style.display = 'none';
        if (this.elCanvasHost) this.elCanvasHost.style.display = 'block';
        if (this.elLoading) this.elLoading.style.display = 'none';
        return;
      } catch (err) {
        console.warn('[ClassroomTwin] Lỗi khởi tạo WebGL, chuyển sang 2D:', err);
      }
    }
    this.activate2DFallback('Thiết bị không hỗ trợ WebGL hoặc chưa tải được Three.js local.');
  };

  ClassroomTwinApp.prototype.checkWebGLSupport = function () {
    try {
      var canvas = document.createElement('canvas');
      return !!(window.WebGLRenderingContext &&
        (canvas.getContext('webgl') || canvas.getContext('experimental-webgl')));
    } catch (e) {
      return false;
    }
  };

  ClassroomTwinApp.prototype.activate2DFallback = function (reasonText) {
    this.renderMode = '2d';
    this.releaseScene();
    if (this.elCanvasHost) this.elCanvasHost.style.display = 'none';
    if (this.elFallback2D) this.elFallback2D.style.display = 'block';
    if (this.elLoading) this.elLoading.style.display = 'none';
    var noteEl = this.elFallback2D ? this.elFallback2D.querySelector('.dt-2d-reason') : null;
    if (noteEl && reasonText) {
      noteEl.textContent = reasonText;
    }
    this.render2DFallback();
  };

  ClassroomTwinApp.prototype.initWebGL = function () {
    var THREE = window.THREE;
    var width = this.elViewportWrap.clientWidth || 680;
    var height = this.elViewportWrap.clientHeight || 340;

    var scene = new THREE.Scene();
    scene.background = new THREE.Color(0x101d2a); // Bầu trời xanh đêm

    var camera = new THREE.PerspectiveCamera(42, width / height, 0.5, 80);
    var renderer = new THREE.WebGLRenderer({
      antialias: true,
      powerPreference: 'default'
    });

    var maxRatio = Math.min(window.devicePixelRatio || 1, 1.5);
    renderer.setPixelRatio(maxRatio);
    renderer.setSize(width, height);

    this.elCanvasHost.innerHTML = '';
    this.elCanvasHost.appendChild(renderer.domElement);

    var self = this;

    // Ánh sáng môi trường đủ thấy mặt đường khi cả ba đèn đều tắt.
    var ambientLight = new THREE.AmbientLight(0x9cb5ca, 0.62);
    scene.add(ambientLight);
    var skyLight = new THREE.DirectionalLight(0xc7d9e8, 0.7);
    skyLight.position.set(-5, 10, 8);
    scene.add(skyLight);

    this.three.scene = scene;
    this.three.camera = camera;
    this.three.renderer = renderer;
    this.onContextLost = function (e) {
      e.preventDefault();
      self.activate2DFallback('Ngữ cảnh WebGL bị gián đoạn — Đã chuyển sang sơ đồ 2D.');
    };
    renderer.domElement.addEventListener('webglcontextlost', this.onContextLost, false);
    this.three.raycaster = new THREE.Raycaster();
    this.three.pointer = new THREE.Vector2();

    this.buildStreetGeometry();
    this.renderMode = 'webgl';
    this.updateCameraTransform();
    this.sync3DLampsVisual();
  };

  ClassroomTwinApp.prototype.buildStreetGeometry = function () {
    var THREE = window.THREE;
    var scene = this.three.scene;

    // Lòng đường chạy theo trục X; ba cột đặt dọc vỉa hè phía xa.
    var asphalt = new THREE.Mesh(
      new THREE.BoxGeometry(14.2, 0.16, 4.6),
      new THREE.MeshLambertMaterial({ color: 0x293744 })
    );
    asphalt.position.set(0, -0.09, 0.75);
    scene.add(asphalt);

    var pavementMat = new THREE.MeshLambertMaterial({ color: 0x697b82 });
    var curbMat = new THREE.MeshLambertMaterial({ color: 0xabb7b2 });
    [-2.25, 3.75].forEach(function (z) {
      var pavement = new THREE.Mesh(new THREE.BoxGeometry(14.2, 0.22, 1.35), pavementMat);
      pavement.position.set(0, 0.02, z);
      scene.add(pavement);
      var curb = new THREE.Mesh(new THREE.BoxGeometry(14.2, 0.18, 0.12), curbMat);
      curb.position.set(0, 0.02, z < 0 ? -1.52 : 3.02);
      scene.add(curb);
    });

    var lineMat = new THREE.MeshBasicMaterial({ color: 0xe6d49e });
    [-5.5, -3.3, -1.1, 1.1, 3.3, 5.5].forEach(function (x) {
      var stripe = new THREE.Mesh(new THREE.BoxGeometry(1.1, 0.012, 0.065), lineMat);
      stripe.position.set(x, 0.005, 0.75);
      scene.add(stripe);
    });

    var poleMat = new THREE.MeshLambertMaterial({ color: 0x91a2a9 });
    var baseMat = new THREE.MeshLambertMaterial({ color: 0x50616b });
    var xPositions = [-4.5, 0, 4.5];
    var self = this;
    this.three.clickableMeshes = [];

    LAMP_IDS.forEach(function (lampId, idx) {
      var xPos = xPositions[idx];
      var group = new THREE.Group();
      group.position.set(xPos, 0, -2.1);

      var footing = new THREE.Mesh(new THREE.CylinderGeometry(0.27, 0.32, 0.22, 12), baseMat);
      footing.position.y = 0.21;
      group.add(footing);

      var pole = new THREE.Mesh(new THREE.CylinderGeometry(0.075, 0.12, 3.65, 12), poleMat);
      pole.position.y = 2.05;
      pole.userData = { lampId: lampId };
      group.add(pole);

      var arm = new THREE.Mesh(new THREE.CylinderGeometry(0.055, 0.055, 1.2, 10), poleMat);
      arm.rotation.x = Math.PI / 2;
      arm.position.set(0, 3.78, 0.57);
      group.add(arm);

      var housingMat = new THREE.MeshLambertMaterial({ color: 0xe2e8f0 });
      var housingMesh = new THREE.Mesh(new THREE.BoxGeometry(0.48, 0.16, 0.68), housingMat);
      housingMesh.position.set(0, 3.71, 1.21);
      housingMesh.userData = { lampId: lampId };
      group.add(housingMesh);

      var bulbMat = new THREE.MeshBasicMaterial({ color: 0x475569 });
      var bulbMesh = new THREE.Mesh(new THREE.BoxGeometry(0.4, 0.04, 0.58), bulbMat);
      bulbMesh.position.set(0, 3.61, 1.21);
      bulbMesh.userData = { lampId: lampId };
      group.add(bulbMesh);

      var ringMat = new THREE.MeshBasicMaterial({ color: 0x38bdf8, transparent: true, opacity: 0 });
      var selectionRing = new THREE.Mesh(new THREE.RingGeometry(0.38, 0.43, 36), ringMat);
      selectionRing.rotation.x = -Math.PI / 2;
      selectionRing.position.set(0, 0.14, 0);
      group.add(selectionRing);

      var coneMat = new THREE.MeshBasicMaterial({
        color: 0xfef08a, transparent: true, opacity: 0,
        side: THREE.DoubleSide, depthWrite: false
      });
      var coneMesh = new THREE.Mesh(new THREE.ConeGeometry(1.18, 3.4, 24, 1, true), coneMat);
      coneMesh.position.set(0, 1.88, 1.21);
      group.add(coneMesh);

      var poolMat = new THREE.MeshBasicMaterial({ color: 0xfef08a, transparent: true, opacity: 0, depthWrite: false });
      var floorPool = new THREE.Mesh(new THREE.CircleGeometry(1.18, 24), poolMat);
      floorPool.rotation.x = -Math.PI / 2;
      floorPool.position.set(xPos, 0.025, -0.89);
      scene.add(floorPool);

      var pointLight = new THREE.PointLight(0xfef08a, 0, 5.5, 1.5);
      pointLight.position.set(xPos, 3.52, -0.89);
      scene.add(pointLight);

      var labelSprite = self.createLabelSprite(LAMP_LABELS[lampId].code + ' · ' + LAMP_LABELS[lampId].zoneName);
      labelSprite.position.set(0, 4.3, 0);
      labelSprite.userData = { lampId: lampId };
      group.add(labelSprite);

      scene.add(group);

      self.three.clickableMeshes.push(pole, housingMesh, bulbMesh, labelSprite);
      self.three.lampObjects[lampId] = {
        group: group,
        housingMat: housingMat,
        bulbMat: bulbMat,
        coneMat: coneMat,
        poolMat: poolMat,
        pointLight: pointLight,
        selectionRingMat: ringMat,
        labelSprite: labelSprite
      };
    });

    this.sync3DLampsVisual();
  };

  ClassroomTwinApp.prototype.createLabelSprite = function (text) {
    var THREE = window.THREE;
    var canvas = document.createElement('canvas');
    canvas.width = 300;
    canvas.height = 75;
    var ctx = canvas.getContext('2d');

    // Nhãn mốc cột đèn trên nền trời tối.
    ctx.fillStyle = 'rgba(16, 29, 42, 0.92)';
    ctx.strokeStyle = '#8db8c5';
    ctx.lineWidth = 4;
    ctx.beginPath();
    ctx.roundRect ? ctx.roundRect(6, 6, 288, 63, 10) : ctx.rect(6, 6, 288, 63);
    ctx.fill();
    ctx.stroke();

    // Chữ đậm màu sáng
    ctx.font = 'bold 26px -apple-system, sans-serif';
    ctx.fillStyle = '#f8fafc';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.fillText(text, 150, 38);

    var texture = new THREE.CanvasTexture(canvas);
    var material = new THREE.SpriteMaterial({ map: texture, depthTest: false });
    var sprite = new THREE.Sprite(material);
    sprite.scale.set(2.2, 0.55, 1);
    return sprite;
  };

  /**
   * Cập nhật hiệu ứng phát sáng 3D cho 3 đèn:
   * - Nhị phân: ON = sáng vàng rực, nón sáng hiện rõ | OFF = tối mờ, nón sáng tắt
   */
  ClassroomTwinApp.prototype.sync3DLampsVisual = function () {
    if (this.renderMode !== 'webgl') {
      this.render2DFallback();
      return;
    }
    var self = this;
    LAMP_IDS.forEach(function (id) {
      var obj = self.three.lampObjects[id];
      var st = self.twinState.lamps[id];
      if (!obj || !st) return;

      var isOn = (st.hasVerifiedData && st.state === 'ON');
      var isDegraded = (!st.available || st.stale || self.twinState.streamStale || !st.hasVerifiedData);
      var isSelected = (self.selectedLampId === id);

      if (isOn) {
        // Bóng đèn phát sáng rực rỡ
        obj.bulbMat.color.setHex(isDegraded ? 0xf59e0b : 0xfef08a);
        obj.coneMat.opacity = isDegraded ? 0.22 : 0.38;
        obj.poolMat.opacity = isDegraded ? 0.26 : 0.44;
        obj.pointLight.intensity = isDegraded ? 0.75 : 1.25;
      } else {
        // Đèn tắt hoàn toàn
        obj.bulbMat.color.setHex(0x475569);
        obj.coneMat.opacity = 0.0;
        obj.poolMat.opacity = 0.0;
        obj.pointLight.intensity = 0.0;
      }

      // Màu vỏ máng đèn: Vàng hổ phách cảnh báo nếu STALE/OFFLINE, trắng bạc nếu bình thường
      obj.housingMat.color.setHex(isDegraded ? 0xf59e0b : 0xe2e8f0);

      // Khung viền Neon cho đèn đang được chọn
      obj.selectionRingMat.opacity = isSelected ? 0.95 : 0.0;
    });
    this.requestRender();
  };

  ClassroomTwinApp.prototype.updateCameraTransform = function () {
    if (!this.three.camera) return;
    var orb = this.cameraOrbit;
    // Giới hạn góc xoay để không nhìn xuyên sàn
    orb.polar = Math.max(0.35, Math.min(1.35, orb.polar));
    orb.azimuth = Math.max(-1.3, Math.min(1.3, orb.azimuth));
    orb.radius = Math.max(8.0, Math.min(22.0, orb.radius));

    var x = orb.target.x + orb.radius * Math.sin(orb.polar) * Math.sin(orb.azimuth);
    var y = orb.target.y + orb.radius * Math.cos(orb.polar);
    var z = orb.target.z + orb.radius * Math.sin(orb.polar) * Math.cos(orb.azimuth);

    this.three.camera.position.set(x, y, z);
    this.three.camera.lookAt(orb.target.x, orb.target.y, orb.target.z);
    this.requestRender();
  };

  ClassroomTwinApp.prototype.resetCamera = function () {
    this.cameraOrbit.azimuth = 0.52;
    this.cameraOrbit.polar = 0.95;
    this.cameraOrbit.radius = 12.8;
    this.updateCameraTransform();
  };

  ClassroomTwinApp.prototype.requestRender = function () {
    if (this.disposed || this.renderMode !== 'webgl' || this.three.animFrameId) return;
    var self = this;
    this.three.animFrameId = requestAnimationFrame(function () {
      self.three.animFrameId = null;
      if (!self.disposed && self.renderMode === 'webgl' && self.three.renderer && self.three.scene && self.three.camera) {
        self.three.renderer.render(self.three.scene, self.three.camera);
      }
    });
  };

  ClassroomTwinApp.prototype.bindEvents = function () {
    var self = this;

    // Nút đặt lại góc nhìn & Chuyển 2D/3D
    if (this.btnResetCam) {
      this.onResetClick = function () { self.resetCamera(); };
      this.btnResetCam.addEventListener('click', this.onResetClick);
    }
    if (this.btnToggle2D) {
      this.onToggle2DClick = function () {
        if (self.renderMode === 'webgl') {
          self.activate2DFallback('Đang bật chế độ sơ đồ 2D thủ công.');
          self.btnToggle2D.textContent = '🧊 Chuyển sang 3D';
        } else {
          self.btnToggle2D.textContent = '📐 Chuyển sang 2D';
          self.initSceneOrFallback();
          self.sync3DLampsVisual();
        }
      };
      this.btnToggle2D.addEventListener('click', this.onToggle2DClick);
    }

    // Xoay Camera Orbit trên Canvas
    var host = this.elCanvasHost;
    var pointerDownPos = { x: 0, y: 0 };

    this.onPointerDown = function (e) {
      self.cameraOrbit.isDragging = true;
      self.cameraOrbit.startX = e.clientX;
      self.cameraOrbit.startY = e.clientY;
      pointerDownPos.x = e.clientX;
      pointerDownPos.y = e.clientY;
    };
    host.addEventListener('pointerdown', this.onPointerDown);

    this.onPointerMove = function (e) {
      if (!self.cameraOrbit.isDragging || self.renderMode !== 'webgl') return;
      var dx = e.clientX - self.cameraOrbit.startX;
      var dy = e.clientY - self.cameraOrbit.startY;
      self.cameraOrbit.startX = e.clientX;
      self.cameraOrbit.startY = e.clientY;
      self.cameraOrbit.azimuth -= dx * 0.007;
      self.cameraOrbit.polar -= dy * 0.007;
      self.updateCameraTransform();
    };
    window.addEventListener('pointermove', this.onPointerMove);

    this.onPointerUp = function (e) {
      if (!self.cameraOrbit.isDragging) return;
      self.cameraOrbit.isDragging = false;
      var moveDist = Math.hypot(e.clientX - pointerDownPos.x, e.clientY - pointerDownPos.y);
      if (moveDist < 6 && self.renderMode === 'webgl') {
        self.handleCanvasPick(e);
      }
    };
    window.addEventListener('pointerup', this.onPointerUp);

    this.onWheel = function (e) {
      if (self.renderMode !== 'webgl') return;
      e.preventDefault();
      self.cameraOrbit.radius += e.deltaY * 0.01;
      self.updateCameraTransform();
    };
    host.addEventListener('wheel', this.onWheel, { passive: false });

    // Tự động căn chỉnh kích thước khi container thay đổi
    this.onResize = function () {
      if (self.renderMode !== 'webgl' || !self.three.renderer || !self.three.camera) return;
      var w = self.elViewportWrap.clientWidth || 680;
      var h = self.elViewportWrap.clientHeight || 340;
      if (w > 0 && h > 0) {
        self.three.camera.aspect = w / h;
        self.three.camera.updateProjectionMatrix();
        self.three.renderer.setSize(w, h);
        self.requestRender();
      }
    };
    window.addEventListener('resize', this.onResize);

    // Kích hoạt ResizeObserver nếu có
    if (window.ResizeObserver && this.elViewportWrap) {
      this.resizeObserver = new ResizeObserver(function () {
        self.onResize();
      });
      this.resizeObserver.observe(this.elViewportWrap);
    }
  };

  ClassroomTwinApp.prototype.handleCanvasPick = function (event) {
    if (!this.three.renderer || !this.three.raycaster || !this.three.camera) return;
    var rect = this.three.renderer.domElement.getBoundingClientRect();
    this.three.pointer.x = ((event.clientX - rect.left) / rect.width) * 2 - 1;
    this.three.pointer.y = -((event.clientY - rect.top) / rect.height) * 2 + 1;

    this.three.raycaster.setFromCamera(this.three.pointer, this.three.camera);
    var intersects = this.three.raycaster.intersectObjects(this.three.clickableMeshes, false);
    if (intersects.length > 0) {
      var pickedId = intersects[0].object.userData.lampId;
      if (pickedId) {
        this.selectLampForInspection(pickedId);
      }
    }
  };

  ClassroomTwinApp.prototype.selectLampForInspection = function (lampId) {
    if (LAMP_IDS.indexOf(lampId) === -1) return;
    this.selectedLampId = lampId;
    this.sync3DLampsVisual();
    this.renderUI();
  };

  ClassroomTwinApp.prototype.applyVerifiedSnapshot = function (payload) {
    if (this.disposed || !payload || typeof payload !== 'object' || !payload.lamps || typeof payload.lamps !== 'object') return;
    var self = this;
    this.twinState.hasSnapshot = true;
    this.twinState.lastReceivedAt = Date.now();
    this.twinState.streamStale = false;
    this.twinState.luxValid = payload.luxValid === true && typeof payload.lux === 'number' && Number.isFinite(payload.lux);
    this.twinState.lux = this.twinState.luxValid ? Math.round(payload.lux * 10) / 10 : null;
    if (Number.isInteger(payload.autoOffLux) && payload.autoOffLux >= 100 && payload.autoOffLux <= 1000) {
      this.twinState.autoOffLux = payload.autoOffLux;
    }
    this.twinState.gatewayPending = payload.gatewayPending === true;
    this.twinState.gatewayStatus = typeof payload.gatewayStatus === 'string' ? payload.gatewayStatus : 'UNKNOWN';
    this.twinState.lastSnapshotTime = typeof payload.verifiedAt === 'number' && Number.isFinite(payload.verifiedAt)
      ? new Date(payload.verifiedAt).toLocaleTimeString('vi-VN', { hour12: false }) : 'Không rõ';

    var rawLamps = payload.lamps || {};
    var normalizedMap = {};

    if (Array.isArray(rawLamps)) {
      rawLamps.forEach(function (item) {
        if (item && item.id) normalizedMap[item.id] = item;
      });
    } else if (typeof rawLamps === 'object') {
      normalizedMap = rawLamps;
    }

    var aliasMap = { 'lamp-01': 'L1', 'lamp-02': 'L2', 'lamp-03': 'L3' };

    LAMP_IDS.forEach(function (id) {
      var src = normalizedMap[id] || normalizedMap[aliasMap[id]];
      var currentLamp = self.twinState.lamps[id];
      if (!src || typeof src !== 'object') {
        currentLamp.available = false;
        currentLamp.stale = true;
        return;
      }
      currentLamp.available = src.available === true;
      currentLamp.stale = src.stale !== false;
      var validState = src.state === 'ON' || src.state === 'OFF';
      var validMode = src.mode === 'AUTO' || src.mode === 'MANUAL';
      var sourceTs = typeof src.sourceTs === 'number' && Number.isFinite(src.sourceTs) ? src.sourceTs : null;
      if (validState && validMode && sourceTs !== null &&
          (!currentLamp.hasVerifiedData || (currentLamp.available && !currentLamp.stale && sourceTs >= currentLamp.sourceTs))) {
        currentLamp.state = src.state;
        currentLamp.mode = src.mode;
        currentLamp.sourceTs = sourceTs;
        currentLamp.hasVerifiedData = true;
        currentLamp.lastUpdatedTs = new Date(sourceTs).toLocaleTimeString('vi-VN', { hour12: false });
        currentLamp.sensorValid = src.sensorValid === true;
        currentLamp.currentMa = currentLamp.sensorValid && typeof src.currentMa === 'number' && Number.isFinite(src.currentMa) ? src.currentMa : null;
        currentLamp.voltageV = currentLamp.sensorValid && typeof src.voltageV === 'number' && Number.isFinite(src.voltageV) ? src.voltageV : null;
      }
    });

    var modes = LAMP_IDS.map(function (id) { return self.twinState.lamps[id].mode; });
    var complete = LAMP_IDS.every(function (id) { return self.twinState.lamps[id].hasVerifiedData; });
    var allAuto = complete && modes.every(function (m) { return m === 'AUTO'; });
    var allManual = complete && modes.every(function (m) { return m === 'MANUAL'; });
    this.twinState.systemModeSummary = !complete ? 'UNKNOWN' : (allAuto ? 'AUTO' : (allManual ? 'MANUAL' : 'MIXED'));

    this.sync3DLampsVisual();
    this.renderUI();
  };

  ClassroomTwinApp.prototype.renderUI = function () {
    var st = this.twinState;
    var T = st.autoOffLux;
    var turnOnLux = T === null ? null : T - 50;

    if (this.elLuxBadge) {
      if (st.luxValid && st.lux !== null && !st.streamStale) {
        var zoneNote = '';
        if (T !== null && st.lux > T) zoneNote = ' (Trên ngưỡng T → AUTO Tắt)';
        else if (T !== null && st.lux < turnOnLux) zoneNote = ' (Dưới T−50 → AUTO Bật)';
        else if (T !== null) zoneNote = ' (Trong vùng trễ → Giữ nguyên)';
        this.elLuxBadge.className = 'dt-top-pill dt-pill-ok';
        this.elLuxBadge.textContent = '☀️ BH1750: ' + st.lux + ' lux [HỢP LỆ]' + zoneNote;
      } else {
        this.elLuxBadge.className = 'dt-top-pill dt-pill-warn';
        this.elLuxBadge.textContent = st.streamStale ? '⚠️ BH1750: Dữ liệu đã cũ' : (st.hasSnapshot ? '⚠️ BH1750: Chưa có lux hợp lệ' : '⏳ BH1750: Chưa có dữ liệu');
      }
    }

    if (this.elThresholdBadge) {
      this.elThresholdBadge.textContent = T === null ? '🎚️ Ngưỡng T: Chưa có dữ liệu' : '🎚️ Ngưỡng T: ' + T + ' lx (Bật < ' + turnOnLux + ' lx | Tắt > ' + T + ' lx)' + (st.streamStale ? ' [dữ liệu cũ]' : '');
    }

    if (this.elModeBadge) {
      if (!st.hasSnapshot || st.streamStale || LAMP_IDS.some(function (id) { return !st.lamps[id].available || st.lamps[id].stale; })) {
        this.elModeBadge.className = 'dt-top-pill dt-pill-warn';
        this.elModeBadge.textContent = '⚠️ Chế độ chung: Chưa xác định / dữ liệu cũ';
      } else if (st.systemModeSummary === 'AUTO') {
        this.elModeBadge.className = 'dt-top-pill dt-pill-auto';
        this.elModeBadge.textContent = '🤖 Chế độ chung: AUTO ( cả 3 đèn )';
      } else if (st.systemModeSummary === 'MANUAL') {
        this.elModeBadge.className = 'dt-top-pill dt-pill-manual';
        this.elModeBadge.textContent = '🖐️ Chế độ chung: MANUAL';
      } else {
        this.elModeBadge.className = 'dt-top-pill dt-pill-mixed';
        this.elModeBadge.textContent = st.systemModeSummary === 'UNKNOWN' ? '⏳ Chế độ chung: Chưa có dữ liệu' : '⚠️ Chế độ chung: MIXED';
      }
    }

    if (this.elBanner) {
      var errorStatuses = ['REJECTED', 'TIMEOUT', 'TELEMETRY_TIMEOUT', 'PUBLISH_FAILED', 'INTERRUPTED', 'REQUEST_REJECTED'];
      if (!st.hasSnapshot || st.streamStale) {
        this.elBanner.className = 'dt-gateway-banner dt-banner-err';
        this.elBanner.textContent = st.hasSnapshot ? '⚠️ Không nhận snapshot mới; đang xem giá trị cuối, không xác nhận ONLINE.' : '⏳ Chưa nhận snapshot đã xác minh.';
      } else if (st.gatewayPending) {
        this.elBanner.className = 'dt-gateway-banner dt-banner-pending';
        this.elBanner.textContent = '⏳ ĐANG CHỜ PHẢN HỒI LỆNH (' + st.gatewayStatus + ')';
      } else if (errorStatuses.indexOf(st.gatewayStatus) !== -1) {
        this.elBanner.className = 'dt-gateway-banner dt-banner-err';
        this.elBanner.textContent = '⚠️ Lệnh thất bại: ' + st.gatewayStatus + ' | Dữ liệu cuối: ' + st.lastSnapshotTime;
      } else {
        this.elBanner.className = 'dt-gateway-banner dt-banner-ok';
        this.elBanner.textContent = '🛡️ Snapshot đã xác minh | Gateway: ' + st.gatewayStatus + ' | Cập nhật cuối: ' + st.lastSnapshotTime;
      }
    }

    var self = this;
    if (this.elCardsWrap) {
      this.elCardsWrap.innerHTML = '';
      LAMP_IDS.forEach(function (id) {
        var lamp = st.lamps[id];
        var meta = LAMP_LABELS[id];
        var isSelected = (self.selectedLampId === id);
        var connBadge = self.getConnectionBadgeInfo(lamp);
        var stateIcon = !lamp.hasVerifiedData ? '❔ CHƯA CÓ DỮ LIỆU' : ((lamp.state === 'ON') ? '💡 BẬT (ON)' : '🌑 TẮT (OFF)');
        var modeIcon = !lamp.hasVerifiedData ? '❔ CHƯA RÕ' : ((lamp.mode === 'AUTO') ? '🤖 AUTO' : '🖐️ MANUAL');

        var card = document.createElement('button');
        card.type = 'button';
        card.className = 'dt-lamp-card' +
          (isSelected ? ' dt-card-selected' : '') +
          ((!lamp.available || lamp.stale || st.streamStale) ? ' dt-card-degraded' : '');

        card.innerHTML =
          '<div class="dt-card-head">' +
            '<strong>' + meta.name + ' (' + meta.code + ')</strong>' +
            '<span class="dt-badge ' + connBadge.cls + '">' + connBadge.text + '</span>' +
          '</div>' +
          '<div class="dt-card-badges">' +
            '<span class="dt-badge ' + (lamp.state === 'ON' ? 'dt-b-on' : 'dt-b-off') + '">' + stateIcon + '</span>' +
            '<span class="dt-badge dt-b-mode">' + modeIcon + '</span>' +
          '</div>' +
          '<div class="dt-card-metrics">' +
            '<span>⚡ ' + (lamp.currentMa !== null ? lamp.currentMa.toFixed(1) + ' mA' : '-- mA') + '</span>' +
            '<span>🔋 ' + (lamp.voltageV !== null ? lamp.voltageV.toFixed(2) + ' V' : '-- V') + '</span>' +
          '</div>';

        card.addEventListener('click', function () {
          self.selectLampForInspection(id);
        });
        self.elCardsWrap.appendChild(card);
      });
    }

    this.renderDetailPane();

    if (this.renderMode === '2d') {
      this.render2DFallback();
    }
  };

  ClassroomTwinApp.prototype.getConnectionBadgeInfo = function (lamp) {
    if (!this.twinState.hasSnapshot || !lamp.hasVerifiedData) return { text: '❔ CHƯA CÓ DỮ LIỆU', cls: 'dt-b-stale' };
    if (this.twinState.streamStale) return { text: '⚠️ DỮ LIỆU CŨ', cls: 'dt-b-stale' };
    if (!lamp.available) return { text: '🔌 OFFLINE', cls: 'dt-b-offline' };
    if (lamp.stale) return { text: '⚠️ STALE', cls: 'dt-b-stale' };
    return { text: '🟢 ONLINE', cls: 'dt-b-online' };
  };

  ClassroomTwinApp.prototype.renderDetailPane = function () {
    if (!this.elDetailPane) return;
    var id = this.selectedLampId;
    var lamp = this.twinState.lamps[id];
    var meta = LAMP_LABELS[id];
    var conn = this.getConnectionBadgeInfo(lamp);
    var isDegraded = (!lamp.available || lamp.stale || this.twinState.streamStale);
    var lastKnown = lamp.hasVerifiedData && isDegraded;

    var warningHtml = '';
    if (!lamp.hasVerifiedData) {
      warningHtml = '<div class="dt-stale-overlay-note">❔ Chưa có trạng thái đèn được xác minh.</div>';
    } else if (isDegraded) {
      warningHtml = '<div class="dt-stale-overlay-note">Đang hiển thị giá trị đã xác minh lần cuối; trạng thái hiện tại chưa rõ.</div>';
    }

    this.elDetailPane.innerHTML =
      '<div class="dt-detail-header">' +
        '<div><strong>🔍 Chi tiết ' + meta.name + ' (' + meta.code + ')</strong> — ' + meta.zoneName + '</div>' +
        '<span class="dt-badge ' + conn.cls + '">' + conn.text + '</span>' +
      '</div>' +
      warningHtml +
      '<div class="dt-detail-grid">' +
        '<div class="dt-detail-item"><span>Trạng thái:</span><strong>' +
          (!lamp.hasVerifiedData ? 'Chưa có dữ liệu' : (lastKnown ? 'Lần cuối: ' : '') + (lamp.state === 'ON' ? '💡 BẬT (ON)' : '🌑 TẮT (OFF)')) +
        '</strong></div>' +
        '<div class="dt-detail-item"><span>Chế độ:</span><strong>' +
          (!lamp.hasVerifiedData ? 'Chưa có dữ liệu' : (lastKnown ? 'Lần cuối: ' : '') + (lamp.mode === 'AUTO' ? '🤖 AUTO' : '🖐️ MANUAL')) +
        '</strong></div>' +
        '<div class="dt-detail-item"><span>Dòng tiêu thụ:</span><strong>' +
          (lamp.currentMa !== null ? lamp.currentMa.toFixed(1) + ' mA' : '--') +
        '</strong></div>' +
        '<div class="dt-detail-item"><span>Điện áp:</span><strong>' +
          (lamp.voltageV !== null ? lamp.voltageV.toFixed(2) + ' V' : '--') +
        '</strong></div>' +
      '</div>';
  };

  ClassroomTwinApp.prototype.render2DFallback = function () {
    var svgHost = this.elFallback2D ? this.elFallback2D.querySelector('.dt-2d-svg-wrap') : null;
    if (!svgHost) return;
    var self = this;
    var cols = [
      { id: 'lamp-01', x: 35 },
      { id: 'lamp-02', x: 215 },
      { id: 'lamp-03', x: 395 }
    ];

    var lampsSvg = cols.map(function (c) {
      var lamp = self.twinState.lamps[c.id];
      var meta = LAMP_LABELS[c.id];
      var isOn = lamp.hasVerifiedData && lamp.state === 'ON';
      var isSel = (self.selectedLampId === c.id);
      var strokeColor = isSel ? '#38bdf8' : '#64748b';
      var coneFill = isOn ? 'rgba(254, 240, 138, 0.30)' : 'none';
      var stateText = !lamp.hasVerifiedData ? '? CHƯA RÕ' : (isOn ? 'ON' : 'OFF');
      var connText = self.getConnectionBadgeInfo(lamp).text;

      return (
        '<g class="dt-2d-lamp-node" data-lamp="' + c.id + '" style="cursor:pointer" role="button" tabindex="0" aria-label="Xem chi tiết ' + meta.name + '">' +
          '<polygon points="' + (c.x + 80) + ',76 ' + (c.x + 112) + ',76 ' + (c.x + 148) + ',196 ' + (c.x + 44) + ',196" fill="' + coneFill + '" />' +
          '<line x1="' + (c.x + 60) + '" y1="154" x2="' + (c.x + 60) + '" y2="51" stroke="' + strokeColor + '" stroke-width="8" />' +
          '<path d="M' + (c.x + 60) + ' 51 h38 v25" fill="none" stroke="' + strokeColor + '" stroke-width="6" />' +
          '<rect x="' + (c.x + 89) + '" y="74" width="24" height="8" rx="2" fill="' + (isOn ? '#fef08a' : '#475569') + '" />' +
          '<circle cx="' + (c.x + 60) + '" cy="154" r="10" fill="#50616b" />' +
          '<text x="' + (c.x + 65) + '" y="25" fill="#f8fafc" font-size="13" font-weight="bold" text-anchor="middle">' + meta.code + ' · ' + stateText + '</text>' +
          '<text x="' + (c.x + 65) + '" y="42" fill="#cbd5e1" font-size="10" text-anchor="middle">' + connText + '</text>' +
        '</g>'
      );
    }).join('');

    svgHost.innerHTML =
      '<svg viewBox="0 0 600 230" width="100%" height="220" style="background:#101d2a;border-radius:6px;border:1px solid #334155" role="img" aria-label="Sơ đồ ba cột đèn đường">' +
        '<rect x="0" y="168" width="600" height="62" fill="#293744"/>' +
        '<rect x="0" y="155" width="600" height="13" fill="#697b82"/>' +
        '<path d="M20 211h75 m50 0h75 m50 0h75 m50 0h75 m50 0h75" stroke="#e6d49e" stroke-width="3"/>' +
        lampsSvg +
      '</svg>';

    var nodes = svgHost.querySelectorAll('.dt-2d-lamp-node');
    Array.prototype.forEach.call(nodes, function (node) {
      node.addEventListener('click', function () {
        self.selectLampForInspection(node.getAttribute('data-lamp'));
      });
      node.addEventListener('keydown', function (event) {
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault();
          self.selectLampForInspection(node.getAttribute('data-lamp'));
        }
      });
    });
  };

  ClassroomTwinApp.prototype.releaseScene = function () {
    if (this.three.animFrameId) {
      cancelAnimationFrame(this.three.animFrameId);
      this.three.animFrameId = null;
    }
    var resources = [];
    if (this.three.scene) this.three.scene.traverse(function (obj) {
      if (obj.geometry) resources.push(obj.geometry);
      var materials = obj.material ? (Array.isArray(obj.material) ? obj.material : [obj.material]) : [];
      materials.forEach(function (mat) {
        if (mat.map) resources.push(mat.map);
        resources.push(mat);
      });
    });
    resources.forEach(function (resource, index) {
      if (resources.indexOf(resource) === index && resource.dispose) resource.dispose();
    });
    if (this.three.renderer) {
      var canvas = this.three.renderer.domElement;
      if (canvas && this.onContextLost) canvas.removeEventListener('webglcontextlost', this.onContextLost);
      if (canvas && canvas.parentNode) canvas.parentNode.removeChild(canvas);
      this.three.renderer.dispose();
    }
    this.three.scene = null;
    this.three.camera = null;
    this.three.renderer = null;
    this.three.clickableMeshes = [];
    this.three.lampObjects = {};
  };

  ClassroomTwinApp.prototype.dispose = function () {
    if (this.disposed) return;
    this.disposed = true;
    window.clearInterval(this.freshnessTimer);
    window.removeEventListener('resize', this.onResize);
    window.removeEventListener('pointermove', this.onPointerMove);
    window.removeEventListener('pointerup', this.onPointerUp);
    if (this.elCanvasHost) {
      this.elCanvasHost.removeEventListener('pointerdown', this.onPointerDown);
      this.elCanvasHost.removeEventListener('wheel', this.onWheel);
    }
    if (this.btnResetCam) this.btnResetCam.removeEventListener('click', this.onResetClick);
    if (this.btnToggle2D) this.btnToggle2D.removeEventListener('click', this.onToggle2DClick);
    if (this.resizeObserver) this.resizeObserver.disconnect();
    this.releaseScene();
  };

  window.ClassroomTwinApp = ClassroomTwinApp;
})(window);
