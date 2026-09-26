if ("RWBigboomTraceStress" in getroottable()) {
    ::RWBigboomTraceStress.Stop();
    getroottable().rawdelete("RWBigboomTraceStress");
}

::RWBigboomTraceStress <- {
    wheel = null,
    brush = null,
    controller = null,
    active = null,
    endTime = 0.0,
    perPulse = 0,
    interval = 0.0,
    pulse = 0,
    totalSpawned = 0,

    // The first point matches the active debris collection's recorded
    // position in the natural capture. Others put debris closer to the
    // rotating collision brush.
    offsets = [
        Vector(22, 826, 17),
        Vector(22, 340, 17),
        Vector(22, -340, 17),
        Vector(110, 0, 35),
        Vector(-110, 0, 35),
        Vector(22, 0, 320)
    ],

    Start = function(explosionsPerPulse = 4, seconds = 2.0,
                     pulseInterval = 0.1) {
        this.Stop();

        this.wheel = Entities.FindByName(null, "water_wheel");
        this.brush = Entities.FindByName(null, "water_wheel_collision");
        if (this.wheel == null || this.brush == null ||
            this.brush.GetMoveParent() != this.wheel ||
            this.brush.GetSolid() == 0 ||
            !this.brush.IsSolidFlagSet(256)) {
            printl("RWBigboomTraceStress: Redwood's root-aligned water-wheel collision brush is unavailable.");
            this.Stop();
            return;
        }

        if (explosionsPerPulse < 1) explosionsPerPulse = 1;
        if (explosionsPerPulse > 8) explosionsPerPulse = 8;
        if (seconds < 0.5) seconds = 0.5;
        if (seconds > 120.0) seconds = 120.0;
        if (pulseInterval < 0.1) pulseInterval = 0.1;
        if (pulseInterval > 1.0) pulseInterval = 1.0;

        this.perPulse = explosionsPerPulse;
        this.interval = pulseInterval;
        this.endTime = Time() + seconds;
        this.active = [];
        this.pulse = 0;
        this.totalSpawned = 0;

        try {
            this.controller = SpawnEntityFromTable("logic_script", {
                targetname = "rw_bigboom_trace_controller"
            });
            if (this.controller == null)
                throw "could not create controller";
            this.controller.ValidateScriptScope();
            this.controller.GetScriptScope().RWBigboomThink <- function() {
                return ::RWBigboomTraceStress.Tick();
            };
            AddThinkToEnt(this.controller, "RWBigboomThink");
            printl("RWBigboomTraceStress: spawning ExplosionCore_MidAir near the live water wheel for " + seconds + " seconds.");
        } catch (err) {
            printl("RWBigboomTraceStress: start failed: " + err);
            this.Stop();
        }
    },

    Tick = function() {
        if (this.wheel == null || !this.wheel.IsValid() ||
            this.brush == null || !this.brush.IsValid() ||
            Time() >= this.endTime) {
            this.Stop();
            return -1;
        }

        // Leave an entity alive long enough for its short-lived child debris
        // to simulate on clients, then reclaim it. No map entity is altered.
        local retained = [];
        foreach (entry in this.active) {
            if (entry.ent != null && entry.ent.IsValid()) {
                if (Time() >= entry.expiry) {
                    entry.ent.AcceptInput("Stop", "", null, null);
                    entry.ent.Destroy();
                } else {
                    retained.append(entry);
                }
            }
        }
        this.active = retained;

        for (local i = 0; i < this.perPulse; ++i) {
            local offset = this.offsets[(this.pulse * this.perPulse + i) % this.offsets.len()];
            local fx = SpawnEntityFromTable("info_particle_system", {
                targetname = "rw_bigboom_trace_effect",
                effect_name = "ExplosionCore_MidAir",
                start_active = 1,
                origin = this.wheel.GetOrigin() + offset
            });
            if (fx == null) {
                printl("RWBigboomTraceStress: particle spawn failed after " + this.totalSpawned + " effects.");
                this.Stop();
                return -1;
            }
            this.active.append({ ent = fx, expiry = Time() + 2.0 });
            ++this.totalSpawned;
        }
        ++this.pulse;
        return this.interval;
    },

    Stop = function() {
        if (this.controller != null && this.controller.IsValid()) {
            AddThinkToEnt(this.controller, null);
            this.controller.Destroy();
        }
        this.controller = null;

        if (this.active != null) {
            foreach (entry in this.active) {
                if (entry.ent != null && entry.ent.IsValid()) {
                    entry.ent.AcceptInput("Stop", "", null, null);
                    entry.ent.Destroy();
                }
            }
        }
        if (this.active != null)
            printl("RWBigboomTraceStress: stopped after " + this.totalSpawned + " effects.");
        this.active = null;
        this.wheel = null;
        this.brush = null;
        this.endTime = 0.0;
        this.perPulse = 0;
        this.interval = 0.0;
        this.pulse = 0;
        this.totalSpawned = 0;
    }
};

::RWBigboomTraceStress.Start();
