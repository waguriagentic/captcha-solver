/**
 * Select — a real listbox, not the platform widget.
 *
 * Radix handles the parts that are easy to get wrong by hand: roving focus,
 * typeahead, Escape/outside-click dismissal, scroll locking, collision-aware
 * positioning, and the aria-activedescendant wiring. Everything visual is ours,
 * built from the same tokens as the rest of the console.
 */
import * as SelectPrimitive from "@radix-ui/react-select";
import { CaretDown, CaretUp, Check } from "@phosphor-icons/react";
import clsx from "clsx";

export type SelectOption = { value: string; label: string; hint?: string };

export function Select({
  value,
  onValueChange,
  options,
  placeholder = "Select…",
  ariaLabel,
  className,
}: {
  value: string;
  onValueChange: (value: string) => void;
  options: SelectOption[];
  placeholder?: string;
  ariaLabel?: string;
  className?: string;
}) {
  return (
    <SelectPrimitive.Root value={value || undefined} onValueChange={onValueChange}>
      <SelectPrimitive.Trigger
        aria-label={ariaLabel}
        className={clsx(
          "group flex h-9.5 w-full items-center justify-between gap-2 rounded-[8px]",
          "border border-line-strong bg-raised px-3 text-left font-mono text-[13px] text-fg",
          "transition-colors duration-150 hover:border-faint",
          "data-[placeholder]:text-faint",
          className,
        )}
      >
        <SelectPrimitive.Value placeholder={placeholder} />
        <SelectPrimitive.Icon asChild>
          <CaretDown
            size={12}
            weight="bold"
            className="shrink-0 text-faint transition-transform duration-150 group-data-[state=open]:rotate-180"
          />
        </SelectPrimitive.Icon>
      </SelectPrimitive.Trigger>

      <SelectPrimitive.Portal>
        <SelectPrimitive.Content
          position="popper"
          sideOffset={4}
          className={clsx(
            "z-50 max-h-[min(60vh,320px)] min-w-[var(--radix-select-trigger-width)]",
            "overflow-hidden rounded-[10px] border border-line-strong bg-panel",
            "shadow-[0_16px_48px_-12px_rgba(0,0,0,0.75)]",
          )}
        >
          <SelectPrimitive.ScrollUpButton className="flex h-6 items-center justify-center text-faint">
            <CaretUp size={12} weight="bold" />
          </SelectPrimitive.ScrollUpButton>
          <SelectPrimitive.Viewport className="p-1">
            {options.map((option) => (
              <SelectPrimitive.Item
                key={option.value}
                value={option.value}
                className={clsx(
                  "relative flex cursor-default items-center gap-2 rounded-[6px] py-1.5 pr-2 pl-7",
                  "text-[13px] text-muted outline-none select-none",
                  "data-[highlighted]:bg-raised data-[highlighted]:text-fg",
                  "data-[state=checked]:text-fg",
                )}
              >
                <SelectPrimitive.ItemIndicator className="absolute left-2 flex items-center">
                  <Check size={12} weight="bold" className="text-accent" />
                </SelectPrimitive.ItemIndicator>
                <SelectPrimitive.ItemText>
                  <span className="font-mono">{option.label}</span>
                </SelectPrimitive.ItemText>
                {option.hint ? (
                  <span className="ml-auto truncate pl-4 text-[11.5px] text-faint">
                    {option.hint}
                  </span>
                ) : null}
              </SelectPrimitive.Item>
            ))}
          </SelectPrimitive.Viewport>
          <SelectPrimitive.ScrollDownButton className="flex h-6 items-center justify-center text-faint">
            <CaretDown size={12} weight="bold" />
          </SelectPrimitive.ScrollDownButton>
        </SelectPrimitive.Content>
      </SelectPrimitive.Portal>
    </SelectPrimitive.Root>
  );
}
