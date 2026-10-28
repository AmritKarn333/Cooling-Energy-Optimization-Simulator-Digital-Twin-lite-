# Primitive 2D Thermal Grid + RL Control

A deliberately simple college-project prototype. It is **not CFD** and should not be interpreted as engineering-grade thermal prediction.

## Components in the sketch

- Black boundary: room.
- Red rectangles (`rack=`): CPU racks, adding heat at a constant rate.
- Green rectangles (`fan=`): directional fans.
- White-outlined rectangle (`cooling=`): the cooling point/line. `mode=fixed` pulls cells toward its target temperature; `mode=exhaust` pulls cells toward ambient. Move it with x/y/w/h.

The cooling point is intentionally modeled as the blue cooling feature from the sketch. You can use fixed-temperature cooling or an exhaust-like sink.

## Requirements

- C++17 compiler (`g++`, MinGW, or clang++)
- Python 3.9+ with standard library only; UI uses Tkinter (often included with Python)

## Run the UI

```bash
python ui.py
```

Use **+ Rack**, **+ Fan**, and **+ Cooling** to add components. Select a component to edit its position, size, rack heat rate, fan direction (`up/down/left/right`) and flow strength, or cooling mode/strength/target temperature. Use **Remove selected component** to delete it. Save and click **Run simulation**. The right panel displays the computed temperature grid and component outlines. Coordinates use a normalized 0..100 room coordinate system.

## Run the C++ simulator directly

```bash
g++ -O2 -std=c++17 thermal_sim.cpp -o thermal_sim
./thermal_sim config.cfg temperature.csv
```

It prints one JSON object containing `average_rack_temp`, `max_rack_temp`, solver step count, and output path. The CSV contains the final temperature grid.

On Windows, compile to `thermal_sim.exe` or run `python ui.py`, which attempts to compile automatically.

## Change config from Python / RL

`train.py` is an example control loop showing the interface. The Python side edits `config.cfg`, calls the executable with `subprocess`, and parses its JSON metrics. Replace its random action with a policy from PPO/DQN or another optimizer.

The interface contract is:
1. Write candidate component coordinates and parameters to `config.cfg`.
2. Run `thermal_sim config.cfg temperature.csv`.
3. Parse JSON metrics from stdout.
4. Use reward, e.g. `-(average_rack_temp + 0.5 * max_rack_temp)`.
5. Repeat for the next action/episode.

## Config syntax

```ini
rack=x,y,w,h,heat_rate
fan=x,y,w,h,direction,strength
cooling=x,y,w,h,mode,strength,target_temp
```

Valid fan directions: `up`, `down`, `left`, `right`. Cooling modes: `fixed`, `exhaust`.

## Simplifications / limitations

- Explicit 2D finite-difference heat diffusion, first-order upwind advection, constant rack heat source, simplified fan corridor, and simplified cooling sink.
- The exhaust mode removes heat toward ambient; fixed mode pulls toward a specified cold target.
- Component coordinates are normalized room coordinates; the temperature grid resolution is configurable.
- This is a prototype. It does not model pressure, turbulence, humidity, server chassis geometry, rack power in watts, or real fan curves.
