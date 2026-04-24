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

    // Setup modal
    showSetup: false,
    setupPass: "",
    setupIface: "enp2s0",
    setupRunning: false,
    setupDone: false,
    setupTermLines: [],
    setupSteps: [
      { label: "Configuring PC IP address...", state: "pending" },
      { label: "Pinging robot at 192.168.0.20...", state: "pending" },
      { label: "Launching RViz in real mode...", state: "pending" },
    ],

    // Plan modal
    showPlanModal: false,
    editMode: false,
    modalName: "",
    modalSteps: [],
    planModalError: "",

    ws: null,

    async init() {
      await this.loadPlans();
      this.pollStatus();
      this.connectWS();
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

          // Parse progress markers emitted by server.py
          if (l.includes("[STEP] Configuring")) {
            this.setupSteps[0].state = "running";
          } else if (l.includes("[STEP] Pinging")) {
            this.setupSteps[0].state = "ok";
            this.setupSteps[1].state = "running";
          } else if (l.includes("[STEP] Launching")) {
            this.setupSteps[1].state = "ok";
            this.setupSteps[2].state = "running";
          } else if (l.includes("[INFO] RViz")) {
            this.setupSteps[2].state = "ok";
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
          } else if (l.includes("[DISCONNECTED]")) {
            this.connected = false;
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

    // ── Plan actions ─────────────────────────────────────────────────────────

    openAddPlan() {
      this.editMode = false;
      this.modalName = "";
      this.modalSteps = [];
      this.planModalError = "";
      this.showPlanModal = true;
    },

    openEditPlan() {
      if (!this.selected) return;
      this.editMode = true;
      this.planModalError = "";
      this.modalName = this.selected.name;
      this.modalSteps = JSON.parse(JSON.stringify(this.selected.steps)).map(
        (s) => ({
          type: s.type,
          pos: [...(s.pos || [0, 0, 0, 0, 0, 0])],
          vel: Array.isArray(s.vel) ? s.vel[0] : (s.vel ?? 30),
          acc: Array.isArray(s.acc) ? s.acc[0] : (s.acc ?? 30),
          time: s.time ?? 2,
        }),
      );
      this.showPlanModal = true;
    },

    addStep() {
      this.modalSteps.push({
        type: "MoveJ",
        pos: [0, 0, 0, 0, 0, 0],
        vel: 30,
        acc: 30,
        time: 2,
      });
    },

    async savePlan() {
      const steps = this.modalSteps.map((s) => {
        const step = { type: s.type, pos: s.pos.map(Number) };
        if (s.vel != null)
          step.vel =
            s.type === "MoveL" ? [Number(s.vel), Number(s.vel)] : Number(s.vel);
        if (s.acc != null)
          step.acc =
            s.type === "MoveL" ? [Number(s.acc), Number(s.acc)] : Number(s.acc);
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
      this.showPlanModal = false;
      await this.loadPlans();
    },

    async importPlan(event) {
      const file = event.target.files[0];
      if (!file) return;
      const fd = new FormData();
      fd.append("file", file);
      const r = await fetch("/api/plans/import", { method: "POST", body: fd });
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
      if (l.includes("[CONNECTED]") || l.includes("[DONE]")) return "sentinel-success";
      if (l.includes("[ERROR]") || l.includes("[DISCONNECTED]")) return "sentinel-error";
      if (l.startsWith("[STEP]") || l.startsWith("[INFO]") || l.startsWith("[STAT]")) return "stat";
      return "log";
    },

    termLineClass(type) {
      if (type === "sentinel-success") return "text-green-700 font-semibold";
      if (type === "sentinel-error")   return "text-red-600 font-semibold";
      if (type === "stat")             return "text-gray-400";
      return "text-green-700"; // log
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
  };
}
