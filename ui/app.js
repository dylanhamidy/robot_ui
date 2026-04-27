function app() {
  return {
    plans: [],
    selected: null,
    connected: false,
    running: false,
    activePlan: null,
    statusMsg: "",
    termLines: [],
    termExpanded: false,
    darkMode: localStorage.getItem('darkMode') === 'true',

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
    planMode: 'manual',
    modalName: "",
    modalSteps: [],
    planModalError: "",
    modalDirty: false,
    showUnsavedWarning: false,

    // Hand teach
    handGuideEnabled: false,
    handGuideLoading: false,
    captureType: 'MoveJ',

    ws: null,

    async init() {
      document.documentElement.classList.toggle('dark', this.darkMode);
      await this.loadPlans();
      this.pollStatus();
      this.connectWS();
    },

    toggleDark() {
      this.darkMode = !this.darkMode;
      localStorage.setItem('darkMode', this.darkMode);
      document.documentElement.classList.toggle('dark', this.darkMode);
    },

    connectWS() {
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
              if (s.state === "running") { s.state = "fail"; break; }
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
              const pos = pt.type === 'MoveJ'
                ? (pt.posj || pt.pos || [0,0,0,0,0,0])
                : (pt.posx || pt.pos || [0,0,0,0,0,0]);
              this.modalSteps.push({
                type: pt.type,
                pos,
                vel: Array.isArray(pt.vel) ? pt.vel[0] : (pt.vel ?? 30),
                acc: Array.isArray(pt.acc) ? pt.acc[0] : (pt.acc ?? 30),
                time: pt.time ?? 2,
              });
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
      this.ws.onclose = () => setTimeout(() => this.connectWS(), 2000);
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
        body: JSON.stringify({ sudo_password: this.setupPass, interface: this.setupIface }),
      });
    },

    // ── Plan modal ───────────────────────────────────────────────────────────

    openAddPlan() {
      this.editMode = false;
      this.planMode = 'manual';
      this.modalName = "";
      this.modalSteps = [];
      this.planModalError = "";
      this.modalDirty = false;
      this.showUnsavedWarning = false;
      this.showPlanModal = true;
    },

    openEditPlan() {
      if (!this.selected) return;
      this.editMode = true;
      this.planMode = 'manual';
      this.planModalError = "";
      this.modalDirty = false;
      this.showUnsavedWarning = false;
      this.modalName = this.selected.name;
      this.modalSteps = JSON.parse(JSON.stringify(this.selected.steps)).map((s) => ({
        type: s.type,
        pos: [...(s.pos || [0, 0, 0, 0, 0, 0])],
        vel: Array.isArray(s.vel) ? s.vel[0] : (s.vel ?? 30),
        acc: Array.isArray(s.acc) ? s.acc[0] : (s.acc ?? 30),
        time: s.time ?? 2,
      }));
      this.showPlanModal = true;
    },

    switchToHandGuide() {
      this.planMode = 'handguide';
    },

    markDirty() {
      this.modalDirty = true;
    },

    addStep() {
      this.modalSteps.push({ type: "MoveJ", pos: [0, 0, 0, 0, 0, 0], vel: 30, acc: 30, time: 2 });
      this.modalDirty = true;
    },

    async savePlan() {
      const steps = this.modalSteps.map((s) => {
        const step = { type: s.type, pos: s.pos.map(Number) };
        if (s.vel != null) step.vel = s.type === "MoveL" ? [Number(s.vel), Number(s.vel)] : Number(s.vel);
        if (s.acc != null) step.acc = s.type === "MoveL" ? [Number(s.acc), Number(s.acc)] : Number(s.acc);
        if (s.time != null) step.time = Number(s.time);
        return step;
      });

      if (this.editMode) {
        await fetch(`/api/plans/${this.selected.name}`, {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ steps }),
        });
      } else {
        const r = await fetch("/api/plans", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name: this.modalName, steps }),
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
      this.showPlanModal = false;
      await this.loadPlans();
    },

    async closePlanModal() {
      if (this.modalDirty) {
        this.showUnsavedWarning = true;
        return;
      }
      if (this.handGuideEnabled) await this.disableHandGuide();
      this.showPlanModal = false;
      this.showUnsavedWarning = false;
    },

    async saveAndClose() {
      await this.savePlan();
      this.showUnsavedWarning = false;
    },

    async confirmDiscard() {
      if (this.handGuideEnabled) this.disableHandGuide();
      this.modalDirty = false;
      this.showUnsavedWarning = false;
      this.showPlanModal = false;
    },

    async importPlan(event) {
      const file = event.target.files[0];
      if (!file) return;
      let body;
      try { body = JSON.parse(await file.text()); } catch (_) {
        this.statusMsg = "Import failed: invalid JSON"; return;
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
      if (!confirm(`Delete plan "${this.selected.name}" and its stats?`)) return;
      await fetch(`/api/plans/${this.selected.name}`, { method: "DELETE" });
      this.selected = null;
      await this.loadPlans();
    },

    classifyLine(l) {
      if (l.includes("[CONNECTED]") || l.includes("[DONE]")) return "sentinel-success";
      if (l.includes("[ERROR]") || l.includes("[DISCONNECTED]")) return "sentinel-error";
      if (l.startsWith("[STEP]")) return "step";
      return "stat";
    },

    termLineClass(type) {
      if (type === "sentinel-success") return "text-green-700 font-semibold";
      if (type === "sentinel-error")   return "text-red-600 font-semibold";
      if (type === "step")             return "text-green-500";
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
      if (!this.editMode && this.modalName.trim() === '') {
        const ts = new Date().toISOString().slice(0, 19).replace('T', '_').replace(/:/g, '-');
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
  };
}
