function app() {
  return {
    currentPage: "robot",
    plans: [],
    selected: null,
    connected: false,
    running: false,
    activePlan: null,
    statusMsg: "",
    termLines: [],
    termExpanded: false,
    darkMode: localStorage.getItem("darkMode") === "true",

    // Turntable
    ttPort: "/dev/ttyACM0",
    ttConnected: false,
    ttEnabled: false,
    ttDirection: "CW",
    ttSpeedPct: 50,
    ttLoading: false,
    ttError: "",
    ttPendingPort: null,
    ttRejectedPorts: [],
    showTtDetectModal: false,
    ttSudoPass: "",
    ttDetectLoading: false,
    ttDetectError: "",
    ttNeedsSudo: false,
    get ttSpeedDelay() {
      // log scale: pct 1→100 maps delay 5000→5 with dense resolution in 5-150μs range
      return Math.round(5 * Math.pow(1000, 1 - this.ttSpeedPct / 100));
    },

    // Modal turntable — independent from page state, exclusive control when activated
    modalTtDirection: "CW",
    modalTtSpeedPct: 50,
    modalTtDuration: 3.0,
    modalTtActivated: false,
    modalTtError: "",
    modalTtLoading: false,
    modalTtParallel: false,
    get modalTtSpeedDelay() {
      return Math.round(5 * Math.pow(1000, 1 - this.modalTtSpeedPct / 100));
    },

    // Setup modal
    showSetup: false,
    setupPass: "",
    setupIface: "enp2s0",
    setupRunning: false,
    setupDone: false,
    setupTermLines: [],
    setupSteps: [
      { label: "Robot workspace", state: "pending" },
      { label: "Configuring PC IP address...", state: "pending" },
      { label: "Pinging robot at 192.168.0.20...", state: "pending" },
      { label: "Launching RViz in real mode...", state: "pending" },
    ],

    // Plan modal
    showPlanModal: false,
    editMode: false,
    planMode: "manual",
    modalName: "",
    modalSteps: [],
    planModalError: "",
    modalDirty: false,
    showUnsavedWarning: false,
    showTtRunningWarning: false,
    selectedStepIndex: null,
    sortableInstance: null,

    // Hand teach
    handGuideEnabled: false,
    handGuideLoading: false,
    captureType: "MoveJ",

    ws: null,

    async init() {
      document.documentElement.classList.toggle("dark", this.darkMode);
      await this.loadPlans();
      this.pollStatus();
      this.pollTurntableStatus();
      this.connectWS();
    },

    toggleDark() {
      this.darkMode = !this.darkMode;
      localStorage.setItem("darkMode", this.darkMode);
      document.documentElement.classList.toggle("dark", this.darkMode);
    },

    connectWS() {
      if (
        this.ws &&
        (this.ws.readyState === WebSocket.OPEN ||
          this.ws.readyState === WebSocket.CONNECTING)
      )
        return;
      const proto = location.protocol === "https:" ? "wss" : "ws";
      this.ws = new WebSocket(`${proto}://${location.host}/ws/terminal`);
      this.ws.onmessage = (e) => {
        const lines = e.data.split("\n");
        for (const l of lines) {
          if (l === "") continue;
          this.termLines.push({ text: l, type: this.classifyLine(l) });
          if (this.termLines.length > 500) this.termLines.shift();
          this.setupTermLines.push(l);
          if (this.setupTermLines.length > 500) this.setupTermLines.shift();

          if (l.includes("[STEP] Checking robot workspace")) {
            this.setupSteps[0].state = "running";
            this.setupSteps[0].label = "Checking robot workspace...";
          } else if (l.includes("[STEP] Building robot workspace")) {
            this.setupSteps[0].label = "Building robot workspace...";
          } else if (l.includes("[INFO] Workspace ready")) {
            this.setupSteps[0].state = "ok";
            this.setupSteps[0].label = "Robot workspace ready";
          } else if (l.includes("[STEP] Configuring")) {
            this.setupSteps[1].state = "running";
          } else if (l.includes("[STEP] Pinging")) {
            this.setupSteps[1].state = "ok";
            this.setupSteps[2].state = "running";
          } else if (l.includes("[STEP] Launching")) {
            this.setupSteps[2].state = "ok";
            this.setupSteps[3].state = "running";
          } else if (l.includes("[INFO] RViz")) {
            this.setupSteps[3].state = "ok";
          } else if (l.includes("[CONNECTED]")) {
            this.connected = true;
            this.setupDone = true;
            this.setupRunning = false;
          } else if (l.includes("[ERROR]")) {
            for (const s of this.setupSteps) {
              if (s.state === "running") {
                s.state = "fail";
                break;
              }
            }
            this.setupDone = true;
            this.setupRunning = false;
          } else if (l.includes("[DONE]")) {
            this.loadPlans();
            this.running = false;
            this.activePlan = null;
          } else if (l.startsWith("[CAPTURE]")) {
            // Node pushed a recorded point — convert to step and add to unified list
            try {
              const pt = JSON.parse(l.slice("[CAPTURE] ".length));
              const pos =
                pt.type === "MoveJ"
                  ? pt.posj || pt.pos || [0, 0, 0, 0, 0, 0]
                  : pt.posx || pt.pos || [0, 0, 0, 0, 0, 0];
              const step = {
                type: pt.type,
                pos,
                vel: Array.isArray(pt.vel) ? pt.vel[0] : (pt.vel ?? 30),
                acc: Array.isArray(pt.acc) ? pt.acc[0] : (pt.acc ?? 30),
                time: pt.time ?? 2,
              };
              if (
                this.selectedStepIndex !== null &&
                this.selectedStepIndex < this.modalSteps.length
              ) {
                this.modalSteps[this.selectedStepIndex] = step;
                this.selectedStepIndex = null;
              } else {
                this.modalSteps.push(step);
              }
              this.modalDirty = true;
            } catch (_) {}
          } else if (l.includes("[PLAN_IMPORTED]")) {
            this.loadPlans();
          } else if (l.includes("[DISCONNECTED]")) {
            this.connected = false;
            this.handGuideEnabled = false;
            this.handGuideLoading = false;
            this.resetSetup();
          }
        }
        this.$nextTick(() => {
          if (this.$refs.termPanel) this.$refs.termPanel.scrollTop = 9999;
          if (this.$refs.setupTerm) this.$refs.setupTerm.scrollTop = 9999;
        });
      };
      this.ws.onclose = () => {
        this.ws = null;
        setTimeout(() => this.connectWS(), 2000);
      };
    },

    async loadPlans() {
      const r = await fetch("/api/plans");
      this.plans = await r.json();
      if (this.selected) {
        const fresh = this.plans.find((p) => p.name === this.selected.name);
        this.selected = fresh || null;
      }
    },

    selectPlan(plan) {
      this.selected = plan;
    },

    async pollStatus() {
      try {
        const r = await fetch("/api/robot/status");
        const s = await r.json();
        this.connected = s.connected;
        this.running = s.running;
        this.activePlan = s.active_plan;
      } catch (_) {}
      setTimeout(() => this.pollStatus(), 2000);
    },

    // ── Setup ────────────────────────────────────────────────────────────────

    resetSetup() {
      this.setupRunning = false;
      this.setupDone = false;
      this.setupTermLines = [];
      this.setupSteps = [
        { label: "Robot workspace", state: "pending" },
        { label: "Configuring PC IP address...", state: "pending" },
        { label: "Pinging robot at 192.168.0.20...", state: "pending" },
        { label: "Launching RViz in real mode...", state: "pending" },
      ];
    },

    async startConnect() {
      this.resetSetup();
      this.setupRunning = true;
      await fetch("/api/robot/connect", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          sudo_password: this.setupPass,
          interface: this.setupIface,
        }),
      });
    },

    // ── Plan modal ───────────────────────────────────────────────────────────

    openAddPlan() {
      this.editMode = false;
      this.planMode = "manual";
      this.modalName = "";
      this.modalSteps = [];
      this.planModalError = "";
      this.modalDirty = false;
      this.showUnsavedWarning = false;
      this.showTtRunningWarning = false;
      this.selectedStepIndex = null;
      this._resetModalTt();
      fetch("/api/robot/hand_guide/points", { method: "DELETE" });
      this.showPlanModal = true;
      this.$nextTick(() => this.initSortable());
    },

    openEditPlan() {
      if (!this.selected) return;
      this.editMode = true;
      this.planMode = "manual";
      this.planModalError = "";
      this.modalDirty = false;
      this.showUnsavedWarning = false;
      this.showTtRunningWarning = false;
      this.selectedStepIndex = null;
      this._resetModalTt();
      fetch("/api/robot/hand_guide/points", { method: "DELETE" });
      this.modalName = this.selected.name;
      const ttParallel = this.selected.turntable_parallel;
      this.modalTtParallel = !!ttParallel;
      if (ttParallel) {
        this.modalTtDirection = ttParallel.direction || "CW";
        this.modalTtSpeedPct = this._pctFromDelay(ttParallel.speed_us || 500);
      }
      this.modalSteps = JSON.parse(JSON.stringify(this.selected.steps)).map((s) => {
        if (s.type === "Turntable") {
          return {
            type: "Turntable",
            direction: s.direction || "CW",
            speed_us: s.speed_us || 500,
            duration: s.duration || 3.0,
            enabled: s.enabled !== false,
          };
        }
        return {
          type: s.type,
          pos: [...(s.pos || [0, 0, 0, 0, 0, 0])],
          vel: Array.isArray(s.vel) ? s.vel[0] : (s.vel ?? 30),
          acc: Array.isArray(s.acc) ? s.acc[0] : (s.acc ?? 30),
          time: s.time ?? 2,
          enabled: s.enabled !== false,
          with_turntable: s.with_turntable || false,
        };
      });
      this.showPlanModal = true;
      this.$nextTick(() => this.initSortable());
    },

    switchToHandGuide() {
      this.planMode = "handguide";
    },

    _resetModalTt() {
      this.modalTtDirection = "CW";
      this.modalTtSpeedPct = 50;
      this.modalTtDuration = 3.0;
      this.modalTtActivated = false;
      this.modalTtError = "";
      this.modalTtLoading = false;
      this.modalTtParallel = false;
    },

    async _cleanupModalTurntable() {
      if (this.modalTtActivated) {
        await fetch("/api/turntable/disable", { method: "POST" }).catch(() => {});
        this.modalTtActivated = false;
      }
    },

    async modalTtEnableMotor() {
      this.modalTtLoading = true;
      this.modalTtError = "";
      try {
        if (this.ttEnabled) {
          await fetch("/api/turntable/disable", { method: "POST" });
        }
        await fetch("/api/turntable/direction", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ direction: this.modalTtDirection }),
        });
        await fetch("/api/turntable/speed", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ delay_us: this.modalTtSpeedDelay }),
        });
        const r = await fetch("/api/turntable/enable", { method: "POST" });
        if (r.ok) {
          this.modalTtActivated = true;
          this.modalTtError = "";
        } else {
          this.modalTtError = (await r.json()).detail;
        }
      } catch (_) {
        this.modalTtError = "Request failed — is the server running?";
      }
      this.modalTtLoading = false;
    },

    async modalTtDisableMotor() {
      this.modalTtLoading = true;
      this.modalTtError = "";
      const r = await fetch("/api/turntable/disable", { method: "POST" });
      if (!r.ok) this.modalTtError = (await r.json()).detail;
      this.modalTtLoading = false;
    },

    async modalTtSetDirection(dir) {
      this.modalTtDirection = dir;
      if (!this.modalTtActivated) return;
      const r = await fetch("/api/turntable/direction", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ direction: dir }),
      });
      if (!r.ok) this.modalTtError = (await r.json()).detail;
    },

    async modalTtSendSpeed() {
      if (!this.modalTtActivated) return;
      const r = await fetch("/api/turntable/speed", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ delay_us: this.modalTtSpeedDelay }),
      });
      if (!r.ok) this.modalTtError = (await r.json()).detail;
    },

    async modalTtSetPreset(pct) {
      this.modalTtSpeedPct = pct;
      await this.modalTtSendSpeed();
    },

    async modalTtSetDelayFromInput(val) {
      const delay = Math.max(5, Math.min(5000, parseInt(val) || 500));
      this.modalTtSpeedPct = this._pctFromDelay(delay);
      await this.modalTtSendSpeed();
    },

    addTurntableStep() {
      this.modalSteps.push({
        type: "Turntable",
        direction: this.modalTtDirection,
        speed_us: this.modalTtSpeedDelay,
        duration: Number(this.modalTtDuration) || 3.0,
        enabled: true,
      });
      this.markDirty();
      this.$nextTick(() => {
        const last = this.$refs.stepsContainer?.lastElementChild;
        if (last) last.scrollIntoView({ behavior: "smooth", block: "nearest" });
      });
    },

    markDirty() {
      this.modalDirty = true;
    },

    addStep() {
      this.modalSteps.push({
        type: "MoveJ",
        pos: [0, 0, 0, 0, 0, 0],
        vel: 30,
        acc: 30,
        time: 2,
        enabled: true,
        with_turntable: false,
      });
      this.modalDirty = true;
      this.$nextTick(() => {
        const last = this.$refs.stepsContainer?.lastElementChild;
        if (last) last.scrollIntoView({ behavior: "smooth", block: "nearest" });
      });
    },

    removeStep(i) {
      this.modalSteps.splice(i, 1);
      if (this.selectedStepIndex === i) {
        this.selectedStepIndex = null;
      } else if (
        this.selectedStepIndex !== null &&
        this.selectedStepIndex > i
      ) {
        this.selectedStepIndex--;
      }
      this.markDirty();
    },

    toggleParallelMode() {
      this.modalTtParallel = !this.modalTtParallel;
      this.modalSteps.forEach(s => {
        if (s.type === "Turntable") s.enabled = !this.modalTtParallel;
      });
      this.markDirty();
    },

    onStepTypeChange(i, step) {
      if (step.type === "Turntable") {
        step.direction = this.modalTtDirection || "CW";
        step.speed_us = this.modalTtSpeedDelay || 500;
        step.duration = this.modalTtDuration || 3.0;
        delete step.pos;
        delete step.vel;
        delete step.acc;
        delete step.time;
        delete step.with_turntable;
      } else {
        step.pos = [0, 0, 0, 0, 0, 0];
        step.vel = 30;
        step.acc = 30;
        step.time = 2;
        step.with_turntable = false;
        delete step.direction;
        delete step.speed_us;
        delete step.duration;
      }
      this.markDirty();
    },

    toggleStepEnabled(i) {
      this.modalSteps[i].enabled = !this.modalSteps[i].enabled;
      this.markDirty();
    },

    selectStep(i) {
      if (this.selectedStepIndex === i) {
        this.selectedStepIndex = null;
        return;
      }
      this.selectedStepIndex = i;
      const step = this.modalSteps[i];
      if (step && step.type !== this.captureType) {
        this.setMoveType(step.type);
      }
    },

    initSortable() {
      if (!window.Sortable) return;
      if (this.sortableInstance) {
        this.sortableInstance.destroy();
        this.sortableInstance = null;
      }
      const el = this.$refs.stepsContainer;
      if (!el) return;
      this.sortableInstance = Sortable.create(el, {
        handle: ".drag-handle",
        draggable: ".step-row",
        animation: 150,
        ghostClass: "sortable-ghost",
        onEnd: (evt) => {
          if (evt.oldIndex === evt.newIndex) return;
          const moved = this.modalSteps.splice(evt.oldIndex, 1)[0];
          this.modalSteps.splice(evt.newIndex, 0, moved);
          if (this.selectedStepIndex === evt.oldIndex) {
            this.selectedStepIndex = evt.newIndex;
          } else if (this.selectedStepIndex !== null) {
            if (
              evt.oldIndex < this.selectedStepIndex &&
              evt.newIndex >= this.selectedStepIndex
            ) {
              this.selectedStepIndex--;
            } else if (
              evt.oldIndex > this.selectedStepIndex &&
              evt.newIndex <= this.selectedStepIndex
            ) {
              this.selectedStepIndex++;
            }
          }
          this.markDirty();
        },
      });
    },

    async savePlan() {
      const steps = this.modalSteps.map((s) => {
        if (s.type === "Turntable") {
          return {
            type: "Turntable",
            direction: s.direction || "CW",
            speed_us: Number(s.speed_us) || 500,
            duration: Number(s.duration) || 3.0,
            enabled: s.enabled !== false,
          };
        }
        const step = { type: s.type, pos: s.pos.map(Number) };
        if (s.vel != null)
          step.vel =
            s.type === "MoveL" ? [Number(s.vel), Number(s.vel)] : Number(s.vel);
        if (s.acc != null)
          step.acc =
            s.type === "MoveL" ? [Number(s.acc), Number(s.acc)] : Number(s.acc);
        if (s.time != null) step.time = Number(s.time);
        step.enabled = s.enabled !== false;
        if (this.modalTtParallel) step.with_turntable = s.with_turntable || false;
        return step;
      });
      const extraPlanFields = this.modalTtParallel
        ? { turntable_parallel: { direction: this.modalTtDirection, speed_us: this.modalTtSpeedDelay } }
        : {};

      if (this.editMode) {
        await fetch(`/api/plans/${this.selected.name}`, {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ steps, ...extraPlanFields }),
        });
      } else {
        if (!this.modalName.trim()) {
          const now = new Date();
          const pad = (n) => String(n).padStart(2, "0");
          this.modalName = `plan_${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}_${pad(now.getHours())}${pad(now.getMinutes())}${pad(now.getSeconds())}`;
        }
        const r = await fetch("/api/plans", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name: this.modalName, steps, ...extraPlanFields }),
        });
        if (!r.ok) {
          this.planModalError = (await r.json()).detail;
          return;
        }
      }
      this.planModalError = "";
      this.modalDirty = false;
      this.showUnsavedWarning = false;
      if (this.handGuideEnabled) await this.disableHandGuide();
      await this._cleanupModalTurntable();
      this.selectedStepIndex = null;
      this.showPlanModal = false;
      await this.loadPlans();
    },

    async closePlanModal() {
      if (this.handGuideEnabled) await this.disableHandGuide();
      if (this.modalTtActivated && this.ttEnabled) {
        this.showTtRunningWarning = true;
        return;
      }
      if (this.modalDirty) {
        this.showUnsavedWarning = true;
        return;
      }
      await this._cleanupModalTurntable();
      this.selectedStepIndex = null;
      this.showPlanModal = false;
      this.showUnsavedWarning = false;
    },

    addTtStepAndContinueClose() {
      this.addTurntableStep();
      this.showTtRunningWarning = false;
      this.showUnsavedWarning = true;
    },

    async closeTtWarningDiscard() {
      this.showTtRunningWarning = false;
      this.modalDirty = false;
      this.showUnsavedWarning = false;
      await this._cleanupModalTurntable();
      this.selectedStepIndex = null;
      this.showPlanModal = false;
    },

    async saveAndClose() {
      await this.savePlan();
      this.showUnsavedWarning = false;
    },

    async confirmDiscard() {
      if (this.handGuideEnabled) this.disableHandGuide();
      fetch("/api/robot/hand_guide/points", { method: "DELETE" });
      await this._cleanupModalTurntable();
      this.selectedStepIndex = null;
      this.modalDirty = false;
      this.showUnsavedWarning = false;
      this.showPlanModal = false;
    },

    async importPlan(event) {
      const file = event.target.files[0];
      if (!file) return;
      let body;
      try {
        body = JSON.parse(await file.text());
      } catch (_) {
        this.statusMsg = "Import failed: invalid JSON";
        return;
      }
      const r = await fetch("/api/plans/import", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!r.ok) {
        this.statusMsg = "Import failed: " + (await r.json()).detail;
        return;
      }
      await this.loadPlans();
      event.target.value = "";
    },

    async confirmDelete() {
      if (!this.selected) return;
      if (!confirm(`Delete plan "${this.selected.name}" and its stats?`))
        return;
      await fetch(`/api/plans/${this.selected.name}`, { method: "DELETE" });
      this.selected = null;
      await this.loadPlans();
    },

    classifyLine(l) {
      if (l.includes("[CONNECTED]") || l.includes("[DONE]"))
        return "sentinel-success";
      if (l.includes("[ERROR]") || l.includes("[DISCONNECTED]"))
        return "sentinel-error";
      if (l.startsWith("[STEP]")) return "step";
      return "stat";
    },

    termLineClass(type) {
      if (type === "sentinel-success") return "text-green-700 font-semibold";
      if (type === "sentinel-error") return "text-red-600 font-semibold";
      if (type === "step") return "text-green-500";
      return "text-gray-400";
    },

    toggleTerm() {
      this.termExpanded = !this.termExpanded;
      if (this.termExpanded) {
        this.$nextTick(() => {
          if (this.$refs.termPanel) this.$refs.termPanel.scrollTop = 9999;
        });
      }
    },

    // ── Robot control ────────────────────────────────────────────────────────

    async startPlan() {
      if (!this.selected || this.running) return;
      const r = await fetch("/api/robot/start", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ plan_name: this.selected.name }),
      });
      if (r.ok) {
        this.running = true;
        this.activePlan = this.selected.name;
        this.statusMsg = "";
        this.termExpanded = true;
        this.$nextTick(() => {
          if (this.$refs.termPanel) this.$refs.termPanel.scrollTop = 9999;
        });
      } else {
        this.statusMsg = "Start failed: " + (await r.json()).detail;
      }
    },

    async stopPlan() {
      if (!this.running) return;
      await fetch("/api/robot/stop", { method: "POST" });
    },

    async disconnect() {
      await fetch("/api/robot/disconnect", { method: "POST" });
    },

    // ── Hand teach ───────────────────────────────────────────────────────────

    async enableHandGuide() {
      this.handGuideLoading = true;
      this.termExpanded = true;
      const r = await fetch("/api/robot/hand_guide/enable", { method: "POST" });
      if ((await r.json()).ok) this.handGuideEnabled = true;
      this.handGuideLoading = false;
    },

    async disableHandGuide() {
      this.handGuideLoading = true;
      await fetch("/api/robot/hand_guide/disable", { method: "POST" });
      this.handGuideEnabled = false;
      this.handGuideLoading = false;
    },

    async setMoveType(type) {
      this.captureType = type;
      await fetch("/api/robot/hand_guide/type", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ move_type: type }),
      });
    },

    async recordPoint() {
      // Auto-name plan if empty
      if (!this.editMode && this.modalName.trim() === "") {
        const ts = new Date()
          .toISOString()
          .slice(0, 19)
          .replace("T", "_")
          .replace(/:/g, "-");
        this.modalName = `capture_${ts}`;
      }
      this.handGuideLoading = true;
      this.termExpanded = true;
      await fetch("/api/robot/hand_guide/record", { method: "POST" });
      this.handGuideLoading = false;
      // Step appears via [CAPTURE] WS event → pushed to modalSteps
    },

    async clearCapture() {
      this.handGuideLoading = true;
      await fetch("/api/robot/hand_guide/clear", { method: "POST" });
      this.modalSteps = [];
      this.modalDirty = false;
      this.showUnsavedWarning = false;
      this.handGuideLoading = false;
    },

    // ── Turntable ─────────────────────────────────────────────────────────────

    async pollTurntableStatus() {
      try {
        const r = await fetch("/api/turntable/status");
        const s = await r.json();
        this.ttConnected = s.connected;
        this.ttEnabled = s.enabled;
        this.ttDirection = s.direction;
        this.ttSpeedPct = this._pctFromDelay(s.speed);
        this.ttRejectedPorts = s.rejected_ports || [];
        if (s.pending_port && !this.showTtDetectModal) {
          this.ttPendingPort = s.pending_port;
          this.ttSudoPass = "";
          this.ttDetectError = "";
          this.ttNeedsSudo = false;
          this.showTtDetectModal = true;
        }
        if (!s.pending_port && this.showTtDetectModal && !this.ttDetectLoading) {
          this.showTtDetectModal = false;
          this.ttPendingPort = null;
        }
      } catch (_) {}
      setTimeout(() => this.pollTurntableStatus(), 2000);
    },

    async ttConnect() {
      this.ttLoading = true;
      this.ttError = "";
      try {
        const r = await fetch("/api/turntable/connect", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ port: this.ttPort }),
        });
        if (r.status === 403) {
          this.ttPendingPort = this.ttPort;
          this.ttSudoPass = "";
          this.ttDetectError = (await r.json()).detail;
          this.ttNeedsSudo = true;
          this.showTtDetectModal = true;
        } else if (!r.ok) {
          this.ttError = (await r.json()).detail;
        }
      } catch (e) {
        this.ttError = "Request failed — is the server running?";
      }
      this.ttLoading = false;
    },

    async ttDisconnect() {
      this.ttLoading = true;
      this.ttError = "";
      await fetch("/api/turntable/disconnect", { method: "POST" });
      this.ttLoading = false;
    },

    async ttEnableMotor() {
      const r = await fetch("/api/turntable/enable", { method: "POST" });
      if (r.ok) {
        this.ttEnabled = true;
        this.ttError = "";
      } else this.ttError = (await r.json()).detail;
    },

    async ttDisableMotor() {
      const r = await fetch("/api/turntable/disable", { method: "POST" });
      if (r.ok) {
        this.ttEnabled = false;
        this.ttError = "";
      } else this.ttError = (await r.json()).detail;
    },

    async ttSetDirection(dir) {
      this.ttDirection = dir;
      const r = await fetch("/api/turntable/direction", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ direction: dir }),
      });
      if (!r.ok) this.ttError = (await r.json()).detail;
    },

    async ttSendSpeed() {
      const r = await fetch("/api/turntable/speed", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ delay_us: this.ttSpeedDelay }),
      });
      if (!r.ok) this.ttError = (await r.json()).detail;
    },

    async ttSetPreset(pct) {
      this.ttSpeedPct = pct;
      await this.ttSendSpeed();
    },

    _pctFromDelay(delay) {
      const clamped = Math.max(5, Math.min(5000, delay));
      return Math.max(
        1,
        Math.min(
          100,
          Math.round((1 - Math.log(clamped / 5) / Math.log(1000)) * 100),
        ),
      );
    },

    async ttSetDelayFromInput(val) {
      const delay = Math.max(5, Math.min(5000, parseInt(val) || 5));
      this.ttSpeedPct = this._pctFromDelay(delay);
      await this.ttSendSpeed();
    },

    async ttConfirm() {
      this.ttDetectLoading = true;
      this.ttDetectError = "";
      try {
        const r = await fetch("/api/turntable/confirm", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ port: this.ttPendingPort, sudo_password: this.ttSudoPass }),
        });
        if (r.ok) {
          this.showTtDetectModal = false;
          this.ttPendingPort = null;
          this.ttSudoPass = "";
          this.ttNeedsSudo = false;
        } else {
          const err = await r.json();
          if (r.status === 403) {
            this.ttNeedsSudo = true;
          }
          this.ttDetectError = err.detail;
        }
      } catch (_) {
        this.ttDetectError = "Request failed — is the server running?";
      }
      this.ttDetectLoading = false;
    },

    async ttReject() {
      await fetch("/api/turntable/reject", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ port: this.ttPendingPort }),
      });
      this.showTtDetectModal = false;
      this.ttPendingPort = null;
      this.ttSudoPass = "";
      this.ttNeedsSudo = false;
      this.ttDetectError = "";
    },

    ttSelectRejected(port) {
      this.ttPort = port;
    },
  };
}
